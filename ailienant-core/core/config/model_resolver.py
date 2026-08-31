"""core/config/model_resolver.py

Resolves the active BYOM chat model for a given tier, mirroring
``embedding_resolver``. The api layer (api/byom.py) derives and persists the
per-tier ``ModelTarget`` map on every preset apply; this module only reads and
caches it, so the core layer can call the active model directly (no proxy)
without importing the api layer.

Also resolves the RUNTIME-REPORTED capabilities of a target — its real context
window and whether it emits native reasoning — instead of trusting a declared
profile or a hardcoded model-name guess. See ``probe_runtime_capabilities``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from core.config.byom_config import ModelTarget, load_byom_config

logger = logging.getLogger("MODEL_RESOLVER")

# Capability ladder, ascending. A sparse preset (some tiers null) must never crash
# the caller, so a missing tier resolves to the nearest present neighbour — expanding
# outward, ties preferring the higher-capability side. This makes a missing ``small``
# step up (small→medium→big→cloud) and a missing ``cloud`` step down
# (cloud→big→medium→small), always landing on a workable model.
_TIER_ORDER: tuple[str, ...] = ("small", "medium", "big", "cloud")


def _directional_order(tier: str) -> list[str]:
    """Tiers to try for ``tier``, nearest-first, ties preferring higher capability."""
    if tier not in _TIER_ORDER:
        return list(_TIER_ORDER)
    idx = _TIER_ORDER.index(tier)
    return sorted(
        _TIER_ORDER,
        key=lambda t: (abs(_TIER_ORDER.index(t) - idx), -(_TIER_ORDER.index(t) > idx)),
    )

_cached: Optional[Dict[str, ModelTarget]] = None


def _load() -> Dict[str, ModelTarget]:
    global _cached
    if _cached is None:
        try:
            _cached = dict(load_byom_config().chat_models)
        except Exception as exc:  # noqa: BLE001 — never let config I/O break a chat turn
            logger.warning("Chat target load failed: %s", exc)
            _cached = {}
    return _cached


def _normalize_for_chat(target: ModelTarget) -> ModelTarget:
    """Route Ollama chat models through litellm's chat endpoint.

    `ollama/<m>` resolves to Ollama's completion endpoint (`/api/generate`), which
    flattens messages into a raw prompt and never applies the model's chat
    template — leaking ChatML control tokens (e.g. `<|im_start|>`) and degrading
    quality. `ollama_chat/<m>` uses `/api/chat`, which applies the template. This
    normalizes at read time so already-persisted presets are fixed without a
    re-apply. Embeddings keep `ollama/` (there is no `ollama_chat` embed route).
    """
    if target.provider == "ollama" and target.model.startswith("ollama/"):
        return target.model_copy(
            update={"model": "ollama_chat/" + target.model.split("/", 1)[1]}
        )
    return target


def get_chat_target(tier: str = "medium") -> Optional[ModelTarget]:
    """Return the chat ModelTarget for ``tier``, with directional fallback.

    Resolution: the requested tier, else its nearest present neighbour on the
    capability ladder (see ``_directional_order``). Returns None when no preset
    has been applied yet (chat_models is empty), so callers can surface an
    actionable "activate a BYOM preset" message.
    """
    targets = _load()
    if not targets:
        return None
    if tier in targets:
        return _normalize_for_chat(targets[tier])
    for t in _directional_order(tier):
        if t in targets:
            logger.info("Chat tier '%s' unset — falling back to '%s'.", tier, t)
            return _normalize_for_chat(targets[t])
    # Any remaining target (deterministic by sorted key) — handles non-ladder keys.
    first_key = sorted(targets.keys())[0]
    return _normalize_for_chat(targets[first_key])


def get_failover_target(tier: str, exclude_model: str) -> Optional[ModelTarget]:
    """Next callable chat target after ``exclude_model`` dropped, ladder nearest-first.

    Walks ``_directional_order(tier)`` and returns the first configured target whose
    model differs from ``exclude_model`` AND is actually callable — a cloud neighbour
    with no api_key is skipped, because there is nothing to fail over to. Returns
    None when no viable alternative exists, so the caller re-raises the original
    transport drop instead of swallowing it.
    """
    targets = _load()
    if not targets:
        return None
    for t in _directional_order(tier):
        cand = targets.get(t)
        if cand is None:
            continue
        norm = _normalize_for_chat(cand)
        if norm.model == exclude_model:
            continue
        if not norm.is_local and not norm.api_key:
            continue  # cloud neighbour with no key — not callable
        return norm
    return None


def refresh() -> None:
    """Clear the cached chat targets. Called after a BYOM preset is applied."""
    global _cached
    _cached = None
    _caps_cache.clear()
    _num_ctx_cache.clear()
    logger.debug("Chat target cache cleared.")


# ─────────────────────────────────────────────────────────────────────────
# Runtime capability probing — the real window and reasoning support, asked
# of the serving runtime rather than declared or guessed.
#
# `LLMProfile.context_window` (brain/state.py) is a hardware-tier default that
# is almost never bound in practice; when unbound, every caller falls back to
# `DEFAULT_CONTEXT_BUDGET` (8192) regardless of what the model actually
# supports or what Ollama actually serves. A local Ollama target additionally
# applies its OWN silent default (`num_ctx`, 4096 unless the caller asks
# otherwise) which is smaller still — the two numbers this module resolves are
# both needed: the architectural ceiling (for sizing `num_ctx` up) and the
# capability list (for deciding whether native reasoning is real, not
# name-matched against a static substring list that a model like `gemma4`
# simply is not on despite genuinely supporting it).
# ─────────────────────────────────────────────────────────────────────────

_CAPS_PROBE_TIMEOUT_S: float = 5.0


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What the serving runtime reports for one physical model.

    ``context_length`` is the model's ARCHITECTURAL maximum context window
    (Ollama's ``model_info.<family>.context_length``) — NOT the window actually
    being served for a call; that is a separate, per-call ``num_ctx`` decision.
    ``supports_thinking`` reflects the runtime's own declared capability list,
    never a hardcoded model-name heuristic.

    Either field being unknown (``context_length=None`` /
    ``supports_thinking=False`` with no probe having succeeded) means exactly
    that — unknown — and callers must fall back to their existing conservative
    default, never treat it as a confirmed negative.
    """
    context_length: Optional[int]
    supports_thinking: bool


_UNKNOWN_CAPABILITIES = RuntimeCapabilities(context_length=None, supports_thinking=False)

# Keyed by the BARE model name (e.g. "gemma4:e4b"), never by tier or by the
# litellm-prefixed id — several tiers commonly resolve to the same physical
# model (a real, observed AILIENANT configuration), and they must share one
# probe rather than issuing it once per tier.
_caps_cache: Dict[str, RuntimeCapabilities] = {}
_caps_lock = asyncio.Lock()


def _bare_ollama_model_name(litellm_model_id: str) -> str:
    """Strip litellm's ``ollama_chat/`` / ``ollama/`` routing prefix.

    Ollama's own ``/api/show`` endpoint expects the bare model tag
    (``gemma4:e4b``), not the litellm-routed id this module otherwise deals in.
    """
    for prefix in ("ollama_chat/", "ollama/"):
        if litellm_model_id.startswith(prefix):
            return litellm_model_id[len(prefix):]
    return litellm_model_id


async def probe_runtime_capabilities(target: Any) -> RuntimeCapabilities:
    """Probe the serving runtime for ``target``'s real window and capabilities.

    Ollama-only today — the only local runtime AILIENANT talks to directly.
    A non-Ollama or unresolvable target returns :data:`_UNKNOWN_CAPABILITIES`
    rather than guessing; the caller decides the fallback, this function never
    does. Never raises: a probe fault must degrade to unknown, not block a
    chat turn that is waiting on this result — including a malformed or
    partial ``target`` (a test double, a future caller passing the wrong
    type) missing an attribute this function reads. ``Any``-typed rather than
    ``ModelTarget`` for exactly that reason: this function's whole contract is
    "degrade gracefully," which must hold even when the input itself is wrong.
    """
    provider = getattr(target, "provider", None)
    api_base = getattr(target, "api_base", None)
    model_id = getattr(target, "model", None)
    if provider != "ollama" or not api_base or not model_id:
        return _UNKNOWN_CAPABILITIES

    bare_name = _bare_ollama_model_name(model_id)
    async with _caps_lock:
        cached = _caps_cache.get(bare_name)
        if cached is not None:
            return cached
        probed = await _probe_ollama_show(api_base, bare_name)
        _caps_cache[bare_name] = probed
        return probed


async def _probe_ollama_show(api_base: str, bare_name: str) -> RuntimeCapabilities:
    try:
        async with httpx.AsyncClient(timeout=_CAPS_PROBE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{api_base.rstrip('/')}/api/show", json={"model": bare_name},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — a probe fault must degrade, never block a chat turn
        logger.warning(
            "Runtime capability probe failed for %s at %s: %s", bare_name, api_base, exc,
        )
        return _UNKNOWN_CAPABILITIES

    model_info = data.get("model_info") or {}
    context_length: Optional[int] = None
    for key, value in model_info.items():
        # The key is family-prefixed ("gemma4.context_length", "qwen2.context_length")
        # and the family varies per model, so match the suffix rather than a fixed key.
        if key.endswith(".context_length") and isinstance(value, int) and value > 0:
            context_length = value
            break
    capabilities = data.get("capabilities") or []
    supports_thinking = "thinking" in capabilities
    return RuntimeCapabilities(context_length=context_length, supports_thinking=supports_thinking)


# ─────────────────────────────────────────────────────────────────────────
# num_ctx resolution — every local Ollama call was pinned to Ollama's own
# silent default (4096) because nothing in AILIENANT ever set the parameter.
# The model's real architectural capacity (probed above) was consistently far
# larger, so a request could exceed the window Ollama actually served without
# ever exceeding what the model itself supports.
#
# Ollama reloads the ENTIRE model when num_ctx changes between calls, so this
# is resolved once per bare model name and only ever GROWN — never shrunk or
# recomputed on every call, which would otherwise force a reload storm.
# ─────────────────────────────────────────────────────────────────────────

# One measured sample (13.1.3): loading gemma4:e4b at num_ctx=16384 instead of
# Ollama's 4096 default grew the resident model 9.43 GB -> 9.6 GB — roughly
# 170 MB over 12288 extra num_ctx tokens, ~14.5 KB/token of KV-cache overhead
# for that model's architecture. Used only as a conservative RAM-affordability
# estimate when no model-specific measurement exists; refining this per model
# is future work, not a blocker to using a better default than "never set it".
_ESTIMATED_KV_BYTES_PER_TOKEN: int = 14_500

# Ollama's own served default when no caller sets num_ctx. Never resolve BELOW
# this — that would be a regression versus doing nothing.
_OLLAMA_DEFAULT_NUM_CTX: int = 4096

# Hard ceiling on top of whatever the architecture/RAM would otherwise allow,
# so a single request can't commit an unbounded amount of host RAM to one
# model's KV cache. Env-overridable, not a silent constant — see the charter's
# "named, env-overridable policy knob" carve-out for values that are a genuine
# policy choice rather than a derivable fact.
_NUM_CTX_HARD_CEILING: int = int(os.getenv("AILIENANT_NUM_CTX_HARD_CEILING", "32768"))

_num_ctx_cache: Dict[str, int] = {}
_num_ctx_lock = asyncio.Lock()


def _estimate_ram_affordable_num_ctx() -> Optional[int]:
    """Conservative ceiling on how many num_ctx tokens the host's free RAM can
    plausibly absorb. Returns ``None`` (no RAM-based constraint) if the
    hardware reading itself fails — the architectural and hard ceilings still
    apply regardless, so this is an additional safety clamp, not the only one.
    """
    try:
        from shared.hardware import HardwareDetector

        profile = HardwareDetector.detect()
        # Reserve half of free RAM for the rest of the running application and
        # the OS rather than committing all of it to one model's KV cache.
        affordable_bytes = (profile.ram_available_gb * 1024**3) * 0.5
        extra_tokens = int(affordable_bytes / _ESTIMATED_KV_BYTES_PER_TOKEN)
        return _OLLAMA_DEFAULT_NUM_CTX + max(0, extra_tokens)
    except Exception:  # noqa: BLE001 — a hardware-read fault must not block resolution
        logger.debug("RAM-affordable num_ctx estimate failed (non-fatal)", exc_info=True)
        return None


class LocalResourceExhaustedError(RuntimeError):
    """Raised when a local-tier call is refused pre-flight because this
    machine's free RAM cannot plausibly absorb it — never raised for a cloud
    target, where no local RAM constraint applies."""


# Floor of free RAM (after this call's projected KV-cache footprint) required
# to admit a local-tier request at all. Distinct from `_estimate_ram_affordable_
# num_ctx`'s 50% reservation above: that shapes HOW BIG a window to request;
# this is a hard admission gate that can refuse the call outright rather than
# let the OS start swapping. Env-overridable — a genuine per-deployment policy
# choice, not a derivable fact.
_LOCAL_RAM_SAFETY_FLOOR_MB: int = int(os.getenv("AILIENANT_LOCAL_RAM_SAFETY_FLOOR_MB", "1024"))

_PS_PROBE_TIMEOUT_S: float = 5.0


async def _query_ollama_ps(api_base: str) -> Optional[list[Dict[str, Any]]]:
    """Live snapshot of what Ollama currently has resident (``GET /api/ps``).

    Returns ``None`` on any transport/parse fault — a probe fault must
    degrade to "unknown," never block or corrupt the admission check it
    feeds. Mirrors ``_probe_ollama_show``'s own degrade-on-failure contract.
    """
    try:
        async with httpx.AsyncClient(timeout=_PS_PROBE_TIMEOUT_S) as client:
            resp = await client.get(f"{api_base.rstrip('/')}/api/ps")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — a probe fault must degrade, never block a chat turn
        logger.debug("Ollama /api/ps probe failed at %s: %s", api_base, exc)
        return None
    models = data.get("models")
    return models if isinstance(models, list) else None


async def check_local_admission(target: Any, requested_num_ctx: int) -> None:
    """Pre-flight admission gate for a local-tier call — refuses outright
    rather than letting the OS silently thrash for minutes.

    Raises :class:`LocalResourceExhaustedError` when this machine's current
    free RAM cannot plausibly absorb the call's projected KV-cache footprint
    plus the safety floor. A no-op for a non-local target (no RAM constraint
    applies) or when the hardware/probe reads themselves fail — degrading to
    "admit" on an unreadable signal matches every other best-effort probe in
    this module (never block a call on a broken thermometer).

    When ``/api/ps`` shows this exact model already resident at a SMALLER
    window than ``requested_num_ctx``, Ollama must fully reload it to serve
    the larger window (this module's own resolve_num_ctx docstring: "Ollama
    reloads the ENTIRE model when num_ctx changes") — a reload can transiently
    need memory for both the outgoing and incoming copies, so that model's
    own currently-resident size is added to the projected footprint for this
    check only, never to the served ``num_ctx`` sizing itself.

    A model already resident at a window that ALREADY holds the request needs
    no reload, so its resident bytes are not charged: they are memory the OS
    reports as in-use precisely because the model is loaded and ready. Charging
    them would make a warm, immediately-servable model the hardest one to admit
    — the exact inversion of what this gate exists to prevent.
    """
    if not getattr(target, "is_local", False):
        return
    try:
        from shared.hardware import HardwareDetector

        profile = HardwareDetector.detect()
        available_bytes = profile.ram_available_gb * 1024**3
    except Exception:  # noqa: BLE001 — an unreadable hardware signal must not block the call
        logger.debug("check_local_admission: hardware read failed (non-fatal)", exc_info=True)
        return

    projected_bytes = requested_num_ctx * _ESTIMATED_KV_BYTES_PER_TOKEN

    api_base = getattr(target, "api_base", None)
    model_id = getattr(target, "model", None)
    if api_base and model_id:
        bare_name = _bare_ollama_model_name(model_id)
        ps_models = await _query_ollama_ps(api_base)
        if ps_models is not None:
            for entry in ps_models:
                entry_name = entry.get("name") or entry.get("model")
                if entry_name and _bare_ollama_model_name(str(entry_name)) == bare_name:
                    resident_ctx = entry.get("context_length")
                    # Absent context_length (older Ollama) → assume a reload is
                    # needed: over-reserving can only refuse a call this gate was
                    # unsure about, while under-reserving lets the box thrash.
                    needs_reload = (
                        not isinstance(resident_ctx, int)
                        or resident_ctx < requested_num_ctx
                    )
                    resident_size = entry.get("size")
                    if needs_reload and isinstance(resident_size, int) and resident_size > 0:
                        projected_bytes += resident_size  # reload headroom
                    break

    floor_bytes = _LOCAL_RAM_SAFETY_FLOOR_MB * 1024 * 1024
    if available_bytes - projected_bytes < floor_bytes:
        raise LocalResourceExhaustedError(
            "Not enough free memory for this local model at this context size "
            f"(~{projected_bytes / 1024**3:.1f} GB projected, "
            f"{available_bytes / 1024**3:.1f} GB free) — close other "
            "applications, pick a smaller model tier, or lower max tokens."
        )


async def resolve_num_ctx(target: Any, min_required: int) -> Optional[int]:
    """Resolve a served context window for ``target`` that can hold
    ``min_required`` tokens (the caller's real prompt + max_tokens + margin),
    sized once per bare model name and only ever grown.

    Returns ``None`` for a non-Ollama target, an unresolvable/malformed one, or
    when the runtime probe cannot determine the model's architectural ceiling —
    the caller then omits ``num_ctx`` entirely from the request, which is
    exactly today's behaviour (a safe, unregressed fallback), never a guessed
    number. ``Any``-typed, not ``ModelTarget``, to match
    ``probe_runtime_capabilities``'s own defend-against-anything contract.
    """
    if getattr(target, "provider", None) != "ollama":
        return None
    caps = await probe_runtime_capabilities(target)
    if caps.context_length is None or caps.context_length <= 0:
        return None

    model_id = getattr(target, "model", None)
    if not model_id:
        return None
    bare_name = _bare_ollama_model_name(model_id)
    async with _num_ctx_lock:
        cached = _num_ctx_cache.get(bare_name)
        if cached is not None and cached >= min_required:
            return cached

        ram_ceiling = _estimate_ram_affordable_num_ctx()
        ceiling = min(
            caps.context_length,
            _NUM_CTX_HARD_CEILING,
            ram_ceiling if ram_ceiling is not None else _NUM_CTX_HARD_CEILING,
        )
        resolved = max(_OLLAMA_DEFAULT_NUM_CTX, min(min_required, ceiling))
        if cached is not None:
            resolved = max(resolved, cached)  # never shrink an already-served window
        _num_ctx_cache[bare_name] = resolved
        return resolved
