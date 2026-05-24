"""core/config/embedding_resolver.py — Phase 7.9.B.12.

Single source of truth for "which embedding backend do we call right now".

Provider-agnostic: the active BYOM preset's provider (Ollama, LM Studio, vLLM,
OpenAI, OpenRouter, Anthropic, custom) determines the embedding model and how it
is reached. Both the indexer preflight (core/indexer.py) and the semantic memory
embed call (core/memory/semantic_memory.py) read from here so they never disagree.

No api-layer imports — this module is consumed by core/* and must stay free of
the byom.py ↔ indexer.py cycle. The api layer (api/byom.py) is what *derives* and
persists the EmbeddingTarget; this module only reads/caches it.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from core.config.byom_config import EmbeddingTarget, load_byom_config
from shared.config import (
    LITELLM_PROXY_API_KEY,
    LITELLM_PROXY_BASE_URL,
    OLLAMA_API_BASE,
)

logger = logging.getLogger("EMBEDDING_RESOLVER")

# Provider-default embedding dimensions. Used as a hint for the LanceDB schema;
# the actual stored dim is always verified against the real vector length.
_DIM_OLLAMA_NOMIC = 768
_DIM_OPENAI = 1536
_DIM_PROXY = 1536

_cached: Optional[EmbeddingTarget] = None


def _legacy_proxy_target() -> EmbeddingTarget:
    """Back-compat fallback: route embeddings through the LiteLLM proxy alias.

    Used when no BYOM preset has been applied yet (cfg.embedding is None) and no
    explicit env override is set. Preserves pre-7.9.B.12 behavior for proxy users.
    """
    return EmbeddingTarget(
        model="ailienant/embedding",
        provider="proxy",
        api_base=LITELLM_PROXY_BASE_URL,
        api_key=LITELLM_PROXY_API_KEY,
        dim=int(os.getenv("AILIENANT_EMBEDDING_DIM", str(_DIM_PROXY))),
        is_local=False,
    )


def _target_from_env_override(model: str) -> EmbeddingTarget:
    """Build a target from an explicit AILIENANT_MODEL_EMBEDDING override.

    The override wins over any persisted preset-derived target (advanced users).
    """
    dim_env = os.getenv("AILIENANT_EMBEDDING_DIM")
    if model.startswith("ailienant/"):
        t = _legacy_proxy_target()
        t.model = model
        return t
    if model.startswith("ollama/"):
        return EmbeddingTarget(
            model=model,
            provider="ollama",
            api_base=OLLAMA_API_BASE,
            api_key="",
            dim=int(dim_env) if dim_env else _DIM_OLLAMA_NOMIC,
            is_local=True,
        )
    # Anything else: treat as an OpenAI-compatible cloud model.
    return EmbeddingTarget(
        model=model,
        provider="openai",
        api_base=None,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        dim=int(dim_env) if dim_env else _DIM_OPENAI,
        is_local=False,
    )


def get_embedding_target() -> EmbeddingTarget:
    """Return the active embedding target (cached).

    Resolution order:
      1. AILIENANT_MODEL_EMBEDDING env override (explicit, advanced).
      2. The preset-derived target persisted in byom_config.json.
      3. Legacy LiteLLM-proxy fallback (back-compat).
    """
    global _cached
    if _cached is not None:
        return _cached

    override = os.getenv("AILIENANT_MODEL_EMBEDDING")
    if override:
        _cached = _target_from_env_override(override)
        logger.info("Embedding target from env override: %s (%s)", _cached.model, _cached.provider)
        return _cached

    try:
        cfg = load_byom_config()
        if cfg.embedding is not None:
            _cached = cfg.embedding
            logger.info(
                "Embedding target from BYOM preset: %s (%s, local=%s, dim=%d)",
                _cached.model, _cached.provider, _cached.is_local, _cached.dim,
            )
            return _cached
    except Exception as exc:  # noqa: BLE001 — never let config I/O break indexing
        logger.warning("Embedding target load failed, using proxy fallback: %s", exc)

    _cached = _legacy_proxy_target()
    logger.info("Embedding target fallback (proxy): %s", _cached.model)
    return _cached


def refresh() -> None:
    """Clear the cached target. Called after a BYOM preset is applied."""
    global _cached
    _cached = None
    logger.debug("Embedding target cache cleared.")
