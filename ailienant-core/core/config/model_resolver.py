"""core/config/model_resolver.py — Phase 7.9.B.13.

Resolves the active BYOM chat model for a given tier, mirroring
``embedding_resolver``. The api layer (api/byom.py) derives and persists the
per-tier ``ModelTarget`` map on every preset apply; this module only reads and
caches it, so the core layer can call the active model directly (no proxy)
without importing the api layer.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

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


def refresh() -> None:
    """Clear the cached chat targets. Called after a BYOM preset is applied."""
    global _cached
    _cached = None
    logger.debug("Chat target cache cleared.")
