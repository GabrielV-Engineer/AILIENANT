"""api/byom.py — Phase 7.9.B.2 / 7.9.B.10.

BYOM (Bring Your Own Model) REST surface for the Web Dashboard.

Endpoints (prefix /api/v1/byom):
  GET  /providers   — provider registry projected for the UI (no secrets).
  GET  /engines     — probe local AI engines (Ollama, LM Studio); return health status.
  POST /test        — probe a model endpoint; returns + imports its discovered models.
  POST /ping        — low-token health check: resolve a model/tier and send a 1-word completion.
  GET  /config      — load BYOM config + built-in presets + the available-model pool.
  PUT  /config      — merge-save BYOM config; (re)apply preset only on explicit activation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

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
from core.config.provider_registry import (
    CLOUD_PROVIDER_IDS,
    PROVIDER_IDS,
    PROVIDER_REGISTRY,
    ProviderSpec,
    get_provider,
    normalize_model_string,
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

# ---------------------------------------------------------------------------
# Embedding-target derivation (Phase 7.9.B.12) — provider-agnostic.
# Per-provider default embed models (overridable via env for power users).
# ---------------------------------------------------------------------------
_OLLAMA_EMBED_MODEL = os.getenv("AILIENANT_OLLAMA_EMBED_MODEL", "ollama/nomic-embed-text")
_LMSTUDIO_EMBED_MODEL = os.getenv("AILIENANT_LMSTUDIO_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
_CUSTOM_EMBED_MODEL = os.getenv("AILIENANT_CUSTOM_EMBED_MODEL", "text-embedding-3-small")
_OPENAI_EMBED_MODEL = os.getenv("AILIENANT_OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Provider identity + routing is owned by the provider registry (single source of
# truth); these projections keep the rest of this module declarative.
_KNOWN_PROVIDERS = set(PROVIDER_IDS)
# Local-first selection: cheap local embeddings preferred, cloud as fallback.
# Locals (in registry order) first, then cloud providers.
_EMBED_PROVIDER_PRIORITY = (
    [p for p in PROVIDER_IDS if PROVIDER_REGISTRY[p].is_local]
    + [p for p in PROVIDER_IDS if not PROVIDER_REGISTRY[p].is_local]
)
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
    endpoint_id: Optional[str] = None  # when set, restores the stored key + caches results


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
    model_cache: dict[str, list[str]] = {}  # endpoint_id → canonical model ids (cloud catalogues)


class EngineStatusItem(BaseModel):
    """Health status of a known local AI engine (Ollama, LM Studio, etc.)."""
    id: str           # "ollama" | "lmstudio"
    name: str         # human-readable label
    url: str          # default base URL for this engine
    running: bool
    model_count: int
    models: list[str]


class PingRequest(BaseModel):
    """Resolve a model and send a minimal completion to verify it works.
    Exactly one of model_id / tier is used (model_id wins)."""
    model_id: Optional[str] = None  # a canonical pool id, e.g. "google/gemini-2.0-flash"
    tier: Optional[str] = None      # an active-preset tier: small | medium | big | cloud


class PingResponse(BaseModel):
    ok: bool
    model: str = ""           # the resolved litellm model string actually called
    reply: str = ""           # truncated model reply (proof of life)
    latency_ms: int = 0
    error: Optional[str] = None


class ProviderSpecOut(BaseModel):
    """Registry provider metadata projected for the dashboard. Carries NO secrets —
    only the env-key *name* (so the UI can hint where a key may already be set).
    Note: no model lists — available models come from testing a configured
    endpoint, never from a system-curated preference."""
    id: str
    label: str
    is_local: bool
    needs_key: bool
    hides_base_url: bool
    default_base_url: Optional[str] = None
    key_hint: str = ""
    help_url: str = ""
    env_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """Auto-prepend http:// to bare hosts; prevents httpx.UnsupportedProtocol."""
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


async def _build_builtin_presets(
    model_cache: Optional[dict[str, list[str]]] = None,
) -> tuple[list[ModelPreset], list[DiscoveredModelItem]]:
    """Compute the 3 built-in template presets + the available-model pool.

    The pool is the union of locally-probed engine models and the cloud models
    a user has imported by testing a configured cloud endpoint (``model_cache``).
    The built-ins are editable starting examples drawn from that real pool.
    """
    raw = await discover_models()
    ollama = [m for m in raw if m["provider"] == "ollama"]

    def _best_ollama(tier: str) -> str:
        for m in ollama:
            if _infer_tier(m["name"]) == tier:
                return f"ollama/{m['name']}"
        return f"ollama/{ollama[0]['name']}" if ollama else ""

    # Cloud models the user actually imported (canonical "provider/model" ids).
    cached_cloud: list[str] = []
    for ids in (model_cache or {}).values():
        for mid in ids:
            if mid not in cached_cloud:
                cached_cloud.append(mid)
    primary_cloud = cached_cloud[0] if cached_cloud else ""

    builtin: list[ModelPreset] = [
        ModelPreset(
            id="builtin-local",
            name="Local Only",
            is_builtin=True,
            description="Example: route every tier to your local Ollama models. Edit to customize.",
            tiers={t: _best_ollama(t) for t in ("small", "medium", "big", "cloud")},
        ),
        ModelPreset(
            id="builtin-hybrid",
            name="Hybrid",
            is_builtin=True,
            description="Example: local small/medium, cloud big/cloud. Edit to customize.",
            tiers={
                "small": _best_ollama("small"),
                "medium": _best_ollama("medium"),
                "big": primary_cloud,
                "cloud": primary_cloud,
            },
        ),
        ModelPreset(
            id="builtin-cloud",
            name="Cloud Only",
            is_builtin=True,
            description="Example: route every tier to an imported cloud model. Edit to customize.",
            tiers={t: primary_cloud for t in ("small", "medium", "big", "cloud")},
        ),
    ]

    # Available-model pool: local engines + imported cloud catalogues (deduped).
    discovered: list[DiscoveredModelItem] = []
    seen: set[str] = set()

    def _add(model_id: str, name: str) -> None:
        if model_id and model_id not in seen:
            seen.add(model_id)
            discovered.append(DiscoveredModelItem(id=model_id, name=name))

    for m in raw:
        if m["provider"] == "ollama":
            _add(f"ollama/{m['name']}", m["name"])
        elif not m["is_local"]:
            _add(m["name"], m["name"])  # env-key fallback entries (no UI endpoint)
    for mid in cached_cloud:
        display = mid.split("/", 1)[1] if "/" in mid else mid
        _add(mid, display)

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


def _canonical_model_id(provider: str, raw_id: str) -> str:
    """Normalize a probed model id to a provider-prefixed canonical id.

    Strips Google's ``models/`` prefix, leaves an already-known provider prefix
    intact, and otherwise prepends the endpoint's provider id — so a preset tier
    value resolves unambiguously even with several cloud endpoints configured.
    """
    name = raw_id.strip()
    if name.startswith("models/"):
        name = name.split("/", 1)[1]
    head = name.split("/", 1)[0]
    if head in _KNOWN_PROVIDERS:
        return name
    return f"{provider}/{name}"


def _merge_patch(existing: BYOMConfig, raw_body: Dict[str, Any]) -> BYOMConfig:
    """Apply only the fields present in raw_body onto existing config.

    Prevents data-loss: a partial payload (e.g. {active_preset_id: "X"})
    never wipes the endpoints, presets, derived targets, or model cache that
    weren't included. Also preserves stored API keys when the client sends back
    masked values.
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

    new_cache = dict(existing.model_cache)
    if "model_cache" in raw_body and isinstance(raw_body["model_cache"], dict):
        for k, v in raw_body["model_cache"].items():
            if isinstance(v, list):
                new_cache[str(k)] = [str(x) for x in v]
    # Drop cache entries whose endpoint no longer exists.
    live_ids = {ep.id for ep in new_endpoints}
    new_cache = {k: v for k, v in new_cache.items() if k in live_ids}

    return BYOMConfig(
        endpoints=new_endpoints,
        presets=new_presets,
        active_preset_id=new_active,
        embedding=existing.embedding,        # carry over derived targets (was dropped)
        chat_models=existing.chat_models,
        model_cache=new_cache,
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
        if e.provider in CLOUD_PROVIDER_IDS or e.provider in ("custom", "vllm")
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
    # Registry cloud providers (Gemini / DeepSeek / Mistral / Qwen / Moonshot / Zhipu):
    # use the provider's own embedding model when it offers one + has a key; otherwise
    # fall back to OpenAI embeddings, then local Ollama, then "(none)" (preflight surfaces it).
    spec = get_provider(provider)
    if spec is not None and not spec.is_local:
        ep = _endpoint_for(provider, endpoints)
        key = _provider_key(spec, ep)
        if spec.embedding_model and key:
            return EmbeddingTarget(
                model=spec.embedding_model, provider=provider,
                api_base=spec.default_base_url if spec.uses_api_base else None,
                api_key=key, dim=_DIM_OPENAI, is_local=False,
            )
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            return EmbeddingTarget(
                model=_OPENAI_EMBED_MODEL, provider="openai",
                api_base=None, api_key=openai_key, dim=_DIM_OPENAI, is_local=False,
            )
        if has_ollama:
            return EmbeddingTarget(
                model=_OLLAMA_EMBED_MODEL, provider="ollama",
                api_base=OLLAMA_API_BASE, api_key="", dim=_DIM_OLLAMA, is_local=True,
            )
        return EmbeddingTarget(
            model="(none)", provider=provider,
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


def _provider_key(spec: ProviderSpec, ep: Optional[EndpointConfig]) -> str:
    """Resolve a provider's api key: stored endpoint key first, then env fallback."""
    if ep and ep.api_key:
        return ep.api_key
    if spec.env_key:
        return os.getenv(spec.env_key, "")
    return ""


def _connection_for_provider(
    provider: str, endpoints: list[EndpointConfig]
) -> tuple[Optional[str], str, bool]:
    """Resolve (api_base, api_key, is_local) for a chat provider from the registry.

    `native` cloud providers (Gemini/DeepSeek/Mistral, OpenAI/Anthropic) carry NO
    api_base — litellm owns the endpoint, which is precisely what keeps a fixed
    cloud URL from being mangled. OpenAI-compatible + local engines carry a base.
    """
    spec = get_provider(provider)
    ep = _endpoint_for(provider, endpoints)
    if spec is None:
        return None, (ep.api_key if ep and ep.api_key else ""), False

    api_base: Optional[str] = None
    if spec.uses_api_base:
        raw = ep.url if ep and ep.url else spec.default_base_url
        if raw:
            api_base = _ensure_v1(raw) if spec.ensure_v1 else raw

    key = _provider_key(spec, ep)
    if not key and spec.is_local and spec.model_style == "openai_compat":
        key = "sk-noauth"  # OpenAI-compatible local servers want a non-empty token
    return api_base, key, spec.is_local


def _normalize_chat_model(model_id: str, provider: str) -> str:
    """Make a tier model id litellm-routable for its provider (registry-driven).

    See `provider_registry.normalize_model_string` for the per-style rules. An
    unknown provider passes the id through unchanged.
    """
    spec = get_provider(provider)
    if spec is None:
        return model_id
    return normalize_model_string(model_id, spec)


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
    """Probe a specific model endpoint and return the list of models it exposes.

    Cloud providers hide their base URL in the UI (it is fixed and known), so the
    probe target comes from the registry's `test_models_url`; local engines probe
    the user-supplied URL.
    """
    spec = get_provider(req.provider)

    # Resolve the probe URL: registry-fixed for cloud, user-supplied for local.
    probe_url: Optional[str]
    probe_kind: str  # "ollama_tags" | "openai_models"
    if spec is not None and not spec.is_local and spec.test_models_url:
        probe_url = spec.test_models_url
        probe_kind = "openai_models"
    elif req.provider == "ollama":
        base = _normalize_url(req.url)
        probe_url = f"{base}/api/tags" if base else None
        probe_kind = "ollama_tags"
    else:
        base = _normalize_url(req.url)
        probe_url = f"{base}/v1/models" if base else None
        probe_kind = "openai_models"

    if not probe_url:
        return TestConnectionResponse(ok=False, models=[], latency_ms=0, error="URL is required")

    # Effective key: form value → stored endpoint key (fixes re-test on a masked
    # field) → provider env fallback.
    key = req.api_key
    if (not key or is_masked_key(key)) and req.endpoint_id:
        cfg_stored = await asyncio.to_thread(load_byom_config)
        stored_ep = next((e for e in cfg_stored.endpoints if e.id == req.endpoint_id), None)
        if stored_ep and stored_ep.api_key:
            key = stored_ep.api_key
    if (not key or is_masked_key(key)) and spec is not None and spec.env_key:
        key = os.getenv(spec.env_key, "")

    headers: dict[str, str] = {}
    if key and not is_masked_key(key):
        if req.provider == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if probe_kind == "ollama_tags":
                resp = await client.get(probe_url, headers=headers)
                resp.raise_for_status()
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                items = [DiscoveredModelItem(id=n, name=n) for n in names if n]
            else:
                resp = await client.get(probe_url, headers=headers)
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

    # One-click import: persist this endpoint's catalogue into the model cache so
    # its models become available to presets + "Detected Models". No preset
    # re-apply / LiteLLM reload here — this is a read-side cache update only.
    if req.endpoint_id and items:
        cfg = await asyncio.to_thread(load_byom_config)
        if any(e.id == req.endpoint_id for e in cfg.endpoints):
            canonical: list[str] = []
            for it in items[:500]:
                cid = _canonical_model_id(req.provider, it.id)
                if cid not in canonical:
                    canonical.append(cid)
            cfg.model_cache[req.endpoint_id] = canonical
            await asyncio.to_thread(save_byom_config, cfg)

    latency = int((time.monotonic() - t0) * 1000)
    return TestConnectionResponse(ok=True, models=items, latency_ms=latency)


@router.get("/config", response_model=BYOMConfigResponse)
async def get_config() -> BYOMConfigResponse:
    """Return the full BYOM config: user endpoints, built-in + user presets, live models."""
    config = await asyncio.to_thread(load_byom_config)
    builtin_presets, discovered = await _build_builtin_presets(config.model_cache)
    return BYOMConfigResponse(
        endpoints=_mask_endpoints(config.endpoints),
        presets=builtin_presets + config.presets,
        active_preset_id=config.active_preset_id,
        discovered=discovered,
        model_cache=config.model_cache,
    )


@router.put("/config", response_model=BYOMConfigResponse)
async def put_config(request: Request) -> BYOMConfigResponse:
    """Merge-save BYOM config, and re-apply the active preset only when the client
    explicitly (re)activates one. A plain endpoints/model_cache save no longer
    rewrites config.yaml or reloads LiteLLM."""
    raw_body: Dict[str, Any] = await request.json()
    existing = await asyncio.to_thread(load_byom_config)
    merged = _merge_patch(existing, raw_body)
    await asyncio.to_thread(save_byom_config, merged)

    # Apply preset → config.yaml → LiteLLM reload — ONLY on an explicit activation.
    if "active_preset_id" in raw_body and merged.active_preset_id:
        builtin_presets, discovered = await _build_builtin_presets(merged.model_cache)
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


@router.post("/ping", response_model=PingResponse)
async def ping_model(req: PingRequest) -> PingResponse:
    """Low-token health check: resolve a model (by canonical id, or by active-preset
    tier) and send a 1-word completion. Proves a key/model/preset actually works
    without burning tokens (``max_tokens=5``)."""
    cfg = await asyncio.to_thread(load_byom_config)

    target: Optional[ModelTarget] = None
    if req.model_id:
        target = _build_chat_target(req.model_id, cfg.endpoints)
    elif req.tier:
        target = model_resolver.get_chat_target(req.tier)

    if target is None or not target.model:
        return PingResponse(ok=False, error="No model resolved — pick a model or activate a preset.")

    import litellm  # deferred — keep module import light

    kwargs: dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "max_tokens": 5,
        "temperature": 0.0,
        "timeout": 20.0,
    }
    if target.api_base:
        kwargs["api_base"] = target.api_base
    if target.api_key:
        kwargs["api_key"] = target.api_key

    t0 = time.monotonic()
    try:
        resp = await litellm.acompletion(**kwargs)
        reply = (resp.choices[0].message.content or "").strip()  # type: ignore[union-attr,index]
        ms = int((time.monotonic() - t0) * 1000)
        return PingResponse(ok=True, model=target.model, reply=reply[:120], latency_ms=ms)
    except Exception as exc:  # noqa: BLE001 — surface any provider/auth error to the UI
        ms = int((time.monotonic() - t0) * 1000)
        return PingResponse(ok=False, model=target.model, latency_ms=ms, error=str(exc)[:300])


@router.get("/providers", response_model=list[ProviderSpecOut])
async def get_providers() -> list[ProviderSpecOut]:
    """Return the provider registry projected for the UI (no secrets).

    The dashboard renders its provider dropdown, per-provider defaults, key hints,
    "get your key" links, model suggestions, and base-URL visibility from this —
    so adding a provider is a backend-only change.
    """
    return [
        ProviderSpecOut(
            id=spec.id,
            label=spec.label,
            is_local=spec.is_local,
            needs_key=spec.needs_key,
            hides_base_url=spec.hides_base_url,
            default_base_url=spec.default_base_url,
            key_hint=spec.key_hint,
            help_url=spec.help_url,
            env_key=spec.env_key,
        )
        for spec in PROVIDER_REGISTRY.values()
    ]


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
