"""api/byom.py — Phase 7.9.B.2 / 7.9.B.10.

BYOM (Bring Your Own Model) REST surface for the Web Dashboard.

Endpoints (prefix /api/v1/byom):
  GET  /engines     — probe local AI engines (Ollama, LM Studio); return health status.
  POST /test        — probe a specific model endpoint; returns discovered models.
  GET  /config      — load BYOM config + 3 built-in presets + currently discovered models.
  PUT  /config      — merge-save BYOM config; apply preset → config.yaml → LiteLLM reload.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config.byom_config import (
    BYOM_CONFIG_PATH,
    BYOMConfig,
    EndpointConfig,
    ModelPreset,
    is_masked_key,
    load_byom_config,
    mask_api_key,
    save_byom_config,
)
from core.config_generator import (
    CONFIG_YAML_PATH,
    LM_STUDIO_API_BASE,
    OLLAMA_API_BASE,
    _infer_tier,
    _probe_lmstudio,
    _probe_ollama,
    discover_models,
    write_config_with_overrides,
)
from shared.config import LITELLM_PROXY_API_KEY, LITELLM_PROXY_BASE_URL
from api.websocket_manager import vfs_manager
from core.indexer import lazy_indexer

logger = logging.getLogger("BYOM_API")
router = APIRouter(prefix="/api/v1/byom", tags=["byom"])


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class TestConnectionRequest(BaseModel):
    url: str
    api_key: str = ""
    provider: str


class DiscoveredModelItem(BaseModel):
    id: str    # model ID to use in preset tiers (e.g. "ollama/phi3", "gpt-4o")
    name: str  # human-readable label


class TestConnectionResponse(BaseModel):
    ok: bool
    models: list[DiscoveredModelItem]
    latency_ms: int
    error: Optional[str] = None


class EndpointConfigOut(BaseModel):
    """EndpointConfig returned to the client — api_key is masked."""
    id: str
    name: str
    url: str
    api_key: str
    provider: str


class BYOMConfigResponse(BaseModel):
    endpoints: list[EndpointConfigOut]
    presets: list[ModelPreset]          # built-ins first, then user-defined
    active_preset_id: Optional[str]
    discovered: list[DiscoveredModelItem]  # all live models for preset dropdowns


class EngineStatusItem(BaseModel):
    """Health status of a known local AI engine (Ollama, LM Studio, etc.)."""
    id: str           # "ollama" | "lmstudio"
    name: str         # human-readable label
    url: str          # default base URL for this engine
    running: bool
    model_count: int
    models: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """Auto-prepend http:// to bare hosts; prevents httpx.UnsupportedProtocol."""
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


async def _build_builtin_presets() -> tuple[list[ModelPreset], list[DiscoveredModelItem]]:
    """Compute the 3 built-in presets from currently live models.

    Also returns a flat list of all discovered model IDs for preset dropdowns.
    """
    raw = await discover_models()

    ollama = [m for m in raw if m["provider"] == "ollama"]
    cloud = [m for m in raw if not m["is_local"]]

    def _best_ollama(tier: str) -> str:
        for m in ollama:
            if _infer_tier(m["name"]) == tier:
                return f"ollama/{m['name']}"
        return f"ollama/{ollama[0]['name']}" if ollama else ""

    cloud_id = f"{cloud[0]['provider']}/{cloud[0]['name']}" if cloud else ""

    builtin: list[ModelPreset] = [
        ModelPreset(
            id="builtin-local",
            name="Local Only",
            is_builtin=True,
            description="Route all tiers to local Ollama models",
            tiers={t: _best_ollama(t) for t in ("small", "medium", "big", "cloud")},
        ),
        ModelPreset(
            id="builtin-hybrid",
            name="Hybrid",
            is_builtin=True,
            description="Local small/medium tiers; cloud big/cloud tier",
            tiers={
                "small": _best_ollama("small"),
                "medium": _best_ollama("medium"),
                "big": cloud_id,
                "cloud": cloud_id,
            },
        ),
        ModelPreset(
            id="builtin-cloud",
            name="Cloud Only",
            is_builtin=True,
            description="Route all tiers to the primary cloud provider",
            tiers={t: cloud_id for t in ("small", "medium", "big", "cloud")},
        ),
    ]

    # Flat list of all live models for the preset-tier dropdowns.
    discovered: list[DiscoveredModelItem] = []
    for m in raw:
        model_id = f"ollama/{m['name']}" if m["provider"] == "ollama" else m["name"]
        discovered.append(DiscoveredModelItem(id=model_id, name=m["name"]))

    return builtin, discovered


def _mask_endpoints(endpoints: list[EndpointConfig]) -> list[EndpointConfigOut]:
    return [
        EndpointConfigOut(
            id=ep.id,
            name=ep.name,
            url=ep.url,
            api_key=mask_api_key(ep.api_key),
            provider=ep.provider,
        )
        for ep in endpoints
    ]


def _merge_patch(existing: BYOMConfig, raw_body: dict) -> BYOMConfig:
    """Apply only the fields present in raw_body onto existing config.

    Prevents data-loss: a partial payload (e.g. {active_preset_id: "X"})
    never wipes the endpoints or presets that weren't included.
    Also preserves stored API keys when the client sends back masked values.
    """
    existing_by_id = {ep.id: ep for ep in existing.endpoints}

    new_endpoints = existing.endpoints
    if "endpoints" in raw_body:
        incoming: list[EndpointConfig] = [
            EndpointConfig.model_validate(e) for e in raw_body["endpoints"]
        ]
        for ep in incoming:
            # If the client echoed a masked key back, keep the real stored key.
            if is_masked_key(ep.api_key) and ep.id in existing_by_id:
                object.__setattr__(ep, "api_key", existing_by_id[ep.id].api_key)
        new_endpoints = incoming

    new_presets = existing.presets
    if "presets" in raw_body:
        # Strip any built-in presets the client may have included (they're
        # always generated server-side; storing them would cause stale data).
        new_presets = [
            ModelPreset.model_validate(p)
            for p in raw_body["presets"]
            if not p.get("is_builtin", False)
        ]

    new_active = existing.active_preset_id
    if "active_preset_id" in raw_body:
        new_active = raw_body["active_preset_id"]

    return BYOMConfig(
        endpoints=new_endpoints,
        presets=new_presets,
        active_preset_id=new_active,
    )


async def _apply_preset(preset: ModelPreset) -> None:
    """Write config.yaml with tier overrides, then signal LiteLLM to reload."""
    tier_overrides: dict[str, str] = {
        f"ailienant/{tier}": model_id
        for tier, model_id in preset.tiers.items()
        if model_id  # skip empty / unset tiers
    }
    await asyncio.to_thread(write_config_with_overrides, CONFIG_YAML_PATH, tier_overrides)

    # Best-effort reload — LiteLLM proxy may not be running.
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(
                f"{LITELLM_PROXY_BASE_URL}/reload",
                headers={"Authorization": f"Bearer {LITELLM_PROXY_API_KEY}"},
            )
            logger.info("LiteLLM /reload response: %s", r.status_code)
    except Exception as exc:
        logger.warning("LiteLLM reload skipped (proxy may be down): %s", exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/test", response_model=TestConnectionResponse)
async def test_connection(req: TestConnectionRequest) -> TestConnectionResponse:
    """Probe a specific model endpoint and return the list of models it exposes."""
    url = _normalize_url(req.url)
    if not url:
        return TestConnectionResponse(ok=False, models=[], latency_ms=0, error="URL is required")

    headers: dict[str, str] = {}
    if req.api_key and not is_masked_key(req.api_key):
        headers["Authorization"] = f"Bearer {req.api_key}"

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if req.provider == "ollama":
                resp = await client.get(f"{url}/api/tags", headers=headers)
                resp.raise_for_status()
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                items = [DiscoveredModelItem(id=n, name=n) for n in names if n]
            else:
                # OpenAI-compatible /v1/models (vllm, openai, openrouter, anthropic, custom)
                resp = await client.get(f"{url}/v1/models", headers=headers)
                resp.raise_for_status()
                data = resp.json().get("data", [])
                items = [
                    DiscoveredModelItem(id=m.get("id", ""), name=m.get("id", ""))
                    for m in data
                    if m.get("id")
                ]
    except httpx.TimeoutException:
        ms = int((time.monotonic() - t0) * 1000)
        return TestConnectionResponse(ok=False, models=[], latency_ms=ms, error="Connection timed out after 5 s")
    except httpx.HTTPStatusError as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return TestConnectionResponse(ok=False, models=[], latency_ms=ms, error=f"HTTP {exc.response.status_code}")
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return TestConnectionResponse(ok=False, models=[], latency_ms=ms, error=str(exc))

    latency = int((time.monotonic() - t0) * 1000)
    return TestConnectionResponse(ok=True, models=items, latency_ms=latency)


@router.get("/config", response_model=BYOMConfigResponse)
async def get_config() -> BYOMConfigResponse:
    """Return the full BYOM config: user endpoints, built-in + user presets, live models."""
    config = await asyncio.to_thread(load_byom_config)
    builtin_presets, discovered = await _build_builtin_presets()
    return BYOMConfigResponse(
        endpoints=_mask_endpoints(config.endpoints),
        presets=builtin_presets + config.presets,
        active_preset_id=config.active_preset_id,
        discovered=discovered,
    )


@router.put("/config", response_model=BYOMConfigResponse)
async def put_config(request: Request) -> BYOMConfigResponse:
    """Merge-save BYOM config and apply the active preset if set."""
    raw_body: dict = await request.json()
    existing = await asyncio.to_thread(load_byom_config)
    merged = _merge_patch(existing, raw_body)
    await asyncio.to_thread(save_byom_config, merged)

    # Apply preset → config.yaml → LiteLLM reload.
    if merged.active_preset_id:
        builtin_presets, _ = await _build_builtin_presets()
        all_presets = {p.id: p for p in builtin_presets + merged.presets}
        active = all_presets.get(merged.active_preset_id)
        if active:
            await _apply_preset(active)
            await vfs_manager.broadcast_byom_config_applied(active.id, active.name)
            await lazy_indexer.retry()

    return await get_config()


@router.get("/engines", response_model=list[EngineStatusItem])
async def get_engine_status() -> list[EngineStatusItem]:
    """Probe known local AI engines and return their health status."""
    ollama_models, lmstudio_models = await asyncio.gather(
        _probe_ollama(), _probe_lmstudio()
    )
    return [
        EngineStatusItem(
            id="ollama",
            name="Ollama",
            url=OLLAMA_API_BASE,
            running=ollama_models is not None,
            model_count=len(ollama_models or []),
            models=ollama_models or [],
        ),
        EngineStatusItem(
            id="lmstudio",
            name="LM Studio",
            url=LM_STUDIO_API_BASE,
            running=lmstudio_models is not None,
            model_count=len(lmstudio_models or []),
            models=lmstudio_models or [],
        ),
    ]
