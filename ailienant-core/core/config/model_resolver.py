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

# Preferred order when the requested tier is unset in the active preset.
_FALLBACK_ORDER = ("medium", "small", "big", "cloud")

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


def get_chat_target(tier: str = "medium") -> Optional[ModelTarget]:
    """Return the chat ModelTarget for ``tier``, with graceful fallback.

    Resolution: requested tier → medium → small → big → cloud → any. Returns
    None when no preset has been applied yet (chat_models is empty), so callers
    can surface an actionable "activate a BYOM preset" message.
    """
    targets = _load()
    if not targets:
        return None
    if tier in targets:
        return targets[tier]
    for t in _FALLBACK_ORDER:
        if t in targets:
            logger.info("Chat tier '%s' unset — falling back to '%s'.", tier, t)
            return targets[t]
    # Any remaining target (deterministic by sorted key).
    first_key = sorted(targets.keys())[0]
    return targets[first_key]


def refresh() -> None:
    """Clear the cached chat targets. Called after a BYOM preset is applied."""
    global _cached
    _cached = None
    logger.debug("Chat target cache cleared.")
