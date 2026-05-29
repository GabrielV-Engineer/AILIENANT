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
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config.byom_config import (
    BYOMConfig,
    EmbeddingTarget,
    EndpointConfig,
    ModelPreset,
    ModelTarget,
    is_masked_key,
    load_byom_config,
    mask_api_key,
    save_byom_config,
)
from core.config import embedding_resolver, model_resolver
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

# ---------------------------------------------------------------------------
# Embedding-target derivation (Phase 7.9.B.12) — provider-agnostic.
# Per-provider default embed models (overridable via env for power users).
# ---------------------------------------------------------------------------
_OLLAMA_EMBED_MODEL = os.getenv("AILIENANT_OLLAMA_EMBED_MODEL", "ollama/nomic-embed-text")
_LMSTUDIO_EMBED_MODEL = os.getenv("AILIENANT_LMSTUDIO_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
_CUSTOM_EMBED_MODEL = os.getenv("AILIENANT_CUSTOM_EMBED_MODEL", "text-embedding-3-small")
_OPENAI_EMBED_MODEL = os.getenv("AILIENANT_OPENAI_EMBED_MODEL", "text-embedding-3-small")

_KNOWN_PROVIDERS = {"ollama", "lmstudio", "vllm", "openai", "openrouter", "anthropic", "custom"}
# Local-first selection: cheap local embeddings preferred, cloud as fallback.
_EMBED_PROVIDER_PRIORITY = ["ollama", "lmstudio", "vllm", "custom", "openai", "openrouter", "anthropic"]
_DIM_OLLAMA = 768
_DIM_OPENAI = 1536

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


def _ensure_v1(url: str) -> str:
    """Normalize an OpenAI-compatible base URL to end in /v1 (LM Studio, vLLM, custom)."""
    u = url.strip().rstrip("/")
    return u if u.endswith("/v1") else f"{u}/v1"


def _provider_of_model(model_id: str, endpoints: list[EndpointConfig]) -> Optional[str]:
    """Infer the provider for a tier's model id.

    Prefixed ids ("ollama/phi3", "openai/gpt-4o") resolve by prefix. A bare cloud
    id ("gpt-4o") is attributed to the single non-local endpoint when unambiguous.
    """
    if not model_id:
        return None
    if "/" in model_id:
        prefix = model_id.split("/", 1)[0].lower()
        if prefix in _KNOWN_PROVIDERS:
            return prefix
    cloud_eps = [
        e for e in endpoints
        if e.provider in ("openai", "openrouter", "anthropic", "custom", "vllm")
    ]
    if len(cloud_eps) == 1:
        return cloud_eps[0].provider
    return None


def _endpoint_for(provider: str, endpoints: list[EndpointConfig]) -> Optional[EndpointConfig]:
    return next((e for e in endpoints if e.provider == provider), None)


def _build_embedding_target(
    provider: str, endpoints: list[EndpointConfig], has_ollama: bool
) -> EmbeddingTarget:
    """Map a chosen provider to a concrete, litellm-ready embedding target."""
    if provider == "ollama":
        ep = _endpoint_for("ollama", endpoints)
        return EmbeddingTarget(
            model=_OLLAMA_EMBED_MODEL, provider="ollama",
            api_base=(ep.url if ep else OLLAMA_API_BASE), api_key="",
            dim=_DIM_OLLAMA, is_local=True,
        )
    if provider == "lmstudio":
        ep = _endpoint_for("lmstudio", endpoints)
        # `base` is reused by the vllm/custom branch below where it may be
        # None (no default endpoint); EmbeddingTarget.api_base is Optional.
        base: str | None = _ensure_v1(ep.url if ep else LM_STUDIO_API_BASE)
        return EmbeddingTarget(
            model=f"openai/{_LMSTUDIO_EMBED_MODEL}", provider="lmstudio",
            api_base=base, api_key=(ep.api_key if ep and ep.api_key else "sk-noauth"),
            dim=_DIM_OLLAMA, is_local=True,
        )
    if provider in ("vllm", "custom"):
        ep = _endpoint_for(provider, endpoints)
        base = _ensure_v1(ep.url) if ep and ep.url else None
        return EmbeddingTarget(
            model=f"openai/{_CUSTOM_EMBED_MODEL}", provider=provider,
            api_base=base, api_key=(ep.api_key if ep and ep.api_key else "sk-noauth"),
            dim=_DIM_OPENAI, is_local=True,
        )
    if provider in ("openai", "openrouter"):
        # OpenRouter has no stable embeddings API → use OpenAI embeddings.
        ep = _endpoint_for("openai", endpoints)
        key = ep.api_key if ep and ep.api_key else os.getenv("OPENAI_API_KEY", "")
        return EmbeddingTarget(
            model=_OPENAI_EMBED_MODEL, provider="openai",
            api_base=None, api_key=key, dim=_DIM_OPENAI, is_local=False,
        )
    if provider == "anthropic":
        # Anthropic has no embeddings API → fallback chain: OpenAI key → local Ollama.
        key = os.getenv("OPENAI_API_KEY", "")
        if key:
            return EmbeddingTarget(
                model=_OPENAI_EMBED_MODEL, provider="openai",
                api_base=None, api_key=key, dim=_DIM_OPENAI, is_local=False,
            )
        if has_ollama:
            return EmbeddingTarget(
                model=_OLLAMA_EMBED_MODEL, provider="ollama",
                api_base=OLLAMA_API_BASE, api_key="", dim=_DIM_OLLAMA, is_local=True,
            )
        # No embedding backend available — preflight surfaces the actionable error.
        return EmbeddingTarget(
            model="(none)", provider="anthropic",
            api_base=None, api_key="", dim=_DIM_OPENAI, is_local=False,
        )
    # Unknown provider → default to local Ollama.
    return EmbeddingTarget(
        model=_OLLAMA_EMBED_MODEL, provider="ollama",
        api_base=OLLAMA_API_BASE, api_key="", dim=_DIM_OLLAMA, is_local=True,
    )


def _derive_embedding_target(
    preset: ModelPreset,
    endpoints: list[EndpointConfig],
    discovered: list["DiscoveredModelItem"],
) -> EmbeddingTarget:
    """Pick the embedding backend that matches the active preset's provider posture."""
    providers: list[str] = []
    for tier_val in preset.tiers.values():
        p = _provider_of_model(tier_val, endpoints)
        if p and p not in providers:
            providers.append(p)
    chosen = next((p for p in _EMBED_PROVIDER_PRIORITY if p in providers), None)
    has_ollama = any(d.id.startswith("ollama/") for d in discovered)
    if chosen is None:
        chosen = "ollama" if has_ollama else "openai"
    return _build_embedding_target(chosen, endpoints, has_ollama)


def _connection_for_provider(
    provider: str, endpoints: list[EndpointConfig]
) -> tuple[Optional[str], str, bool]:
    """Resolve (api_base, api_key, is_local) for a chat provider from BYOM config.

    Local engines carry an api_base; cloud providers carry a key (endpoint or env).
    """
    ep = _endpoint_for(provider, endpoints)
    if provider == "ollama":
        return (ep.url if ep and ep.url else OLLAMA_API_BASE), "", True
    if provider == "lmstudio":
        return _ensure_v1(ep.url if ep and ep.url else LM_STUDIO_API_BASE), (
            ep.api_key if ep and ep.api_key else "sk-noauth"
        ), True
    if provider in ("vllm", "custom"):
        base = _ensure_v1(ep.url) if ep and ep.url else None
        return base, (ep.api_key if ep and ep.api_key else "sk-noauth"), True
    if provider == "openai":
        return None, (ep.api_key if ep and ep.api_key else os.getenv("OPENAI_API_KEY", "")), False
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1", (
            ep.api_key if ep and ep.api_key else os.getenv("OPENROUTER_API_KEY", "")
        ), False
    if provider == "anthropic":
        return None, (ep.api_key if ep and ep.api_key else os.getenv("ANTHROPIC_API_KEY", "")), False
    return None, (ep.api_key if ep and ep.api_key else ""), False


def _normalize_chat_model(model_id: str, provider: str) -> str:
    """Make a tier model id litellm-routable for its provider.

    OpenAI-compatible local servers (LM Studio / vLLM / custom) must be addressed
    via the `openai/<model>` provider with a custom api_base. Ollama chat models
    must use `ollama_chat/<model>` (litellm's `/api/chat` route) so the model's
    chat template is applied — `ollama/<model>` hits `/api/generate` and leaks raw
    template tokens.
    """
    if provider in ("lmstudio", "vllm", "custom"):
        base_name = model_id.split("/", 1)[1] if "/" in model_id else model_id
        return f"openai/{base_name}"
    if provider == "ollama":
        base_name = model_id.split("/", 1)[1] if "/" in model_id else model_id
        return f"ollama_chat/{base_name}"
    return model_id


def _build_chat_target(model_id: str, endpoints: list[EndpointConfig]) -> ModelTarget:
    """Resolve a preset tier's concrete model id into a litellm-ready ModelTarget."""
    provider = _provider_of_model(model_id, endpoints) or ("ollama" if model_id.startswith("ollama/") else "openai")
    api_base, api_key, is_local = _connection_for_provider(provider, endpoints)
    return ModelTarget(
        model=_normalize_chat_model(model_id, provider),
        provider=provider,
        api_base=api_base,
        api_key=api_key,
        is_local=is_local,
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
        builtin_presets, discovered = await _build_builtin_presets()
        all_presets = {p.id: p for p in builtin_presets + merged.presets}
        active = all_presets.get(merged.active_preset_id)
        if active:
            await _apply_preset(active)
            # Phase 7.9.B.12 — derive + persist the provider-agnostic embedding
            # target so the indexer preflight + semantic memory route correctly.
            merged.embedding = _derive_embedding_target(active, merged.endpoints, discovered)
            # Phase 7.9.B.13 — persist per-tier chat targets so the main chat +
            # analyst can call the active model directly (no proxy).
            merged.chat_models = {
                tier: _build_chat_target(model_id, merged.endpoints)
                for tier, model_id in active.tiers.items() if model_id
            }
            await asyncio.to_thread(save_byom_config, merged)
            embedding_resolver.refresh()
            model_resolver.refresh()
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
