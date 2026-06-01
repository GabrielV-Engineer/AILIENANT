"""core/config/byom_config.py — Phase 7.9.B.2.

BYOM (Bring Your Own Model) config schema + hardened file I/O.

Global config file is stored alongside ailienant_catalog.sqlite so its path
is always deterministic, regardless of the process working directory.
"""
from __future__ import annotations

import logging
import os
import pathlib
import stat
import tempfile
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("BYOM_CONFIG")

# ---------------------------------------------------------------------------
# Resolve the global storage directory from the same env var used by the DB.
# This guarantees byom_config.json is co-located with ailienant_catalog.sqlite.
# ---------------------------------------------------------------------------
_CATALOG_PATH: pathlib.Path = pathlib.Path(
    os.environ.get("AILIENANT_CATALOG_DB", "ailienant_catalog.sqlite")
).resolve()
BYOM_CONFIG_PATH: pathlib.Path = _CATALOG_PATH.parent / "byom_config.json"

_MASK_PREFIX = "sk-••••"  # "sk-••••"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class EndpointConfig(BaseModel):
    id: str
    name: str
    url: str
    api_key: str = ""
    provider: Literal["ollama", "lmstudio", "vllm", "openai", "openrouter", "anthropic", "custom"]


class ModelPreset(BaseModel):
    id: str
    name: str
    description: str = ""
    is_builtin: bool = False
    # keys: tier names ("small" | "medium" | "big" | "cloud")
    # values: real litellm_params.model value ("ollama/phi3", "gpt-4o", ...)
    tiers: dict[str, str] = Field(default_factory=dict)


class EmbeddingTarget(BaseModel):
    """Resolved embedding backend, derived from the active preset's provider.

    Persisted so the core layer (indexer preflight + semantic memory) can route
    embeddings without importing the api layer. Provider-agnostic: api_base is
    set for local + custom/openrouter; api_key carries the cloud secret.
    """
    model: str                      # litellm model id, e.g. "ollama/nomic-embed-text"
    provider: str                   # ollama | lmstudio | vllm | openai | openrouter | anthropic | custom
    api_base: Optional[str] = None  # local engine / custom base URL; None for native openai
    api_key: str = ""               # cloud key (byom_config.json is already 0600)
    dim: int = 768                  # expected vector dimension (verified dynamically at write)
    is_local: bool = True


class ModelTarget(BaseModel):
    """A litellm-ready chat model resolved from a preset tier (Phase 7.9.B.13).

    Lets the core layer call the active BYOM chat model directly (no proxy):
    api_base is set for local engines + custom/openrouter; api_key carries the
    cloud secret (byom_config.json is already 0600).
    """
    model: str                      # litellm model id, e.g. "ollama/llama3.1", "gpt-4o"
    provider: str                   # ollama | lmstudio | vllm | openai | openrouter | anthropic | custom
    api_base: Optional[str] = None  # local engine / custom base URL; None for native openai
    api_key: str = ""
    is_local: bool = True


class BYOMConfig(BaseModel):
    endpoints: list[EndpointConfig] = Field(default_factory=list)
    presets: list[ModelPreset] = Field(default_factory=list)
    active_preset_id: Optional[str] = None
    embedding: Optional[EmbeddingTarget] = None  # persisted by _apply_preset (Phase 7.9.B.12)
    chat_models: dict[str, ModelTarget] = Field(default_factory=dict)  # tier → target (Phase 7.9.B.13)


# ---------------------------------------------------------------------------
# API-key masking helpers
# ---------------------------------------------------------------------------


def mask_api_key(key: str) -> str:
    """Return a masked key safe for GET responses.  Empty key → empty string."""
    if not key:
        return ""
    suffix = key[-4:] if len(key) >= 4 else "****"
    return _MASK_PREFIX + suffix


def is_masked_key(value: str) -> bool:
    """True when the value is a round-tripped masked key (not a real secret)."""
    return value.startswith(_MASK_PREFIX)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stream-watchdog governance — the client's give-up timeout is dictated here,
# never hardcoded in the IDE. Local engines (Ollama / LM Studio) cold-load
# weights into VRAM and emit tokens slowly, so they earn a longer leash before
# the UI declares a stream stalled; fast cloud APIs keep a tighter bound.
# ---------------------------------------------------------------------------
_WATCHDOG_LOCAL_MS: int = 180_000
_WATCHDOG_CLOUD_MS: int = 90_000


def stream_watchdog_ms() -> int:
    """Resolve the client stream-watchdog timeout from the active model routing.

    The heavy generation tier governs time-between-tokens, so a local big/medium
    tier extends the leash while a cloud tier keeps it tight. Falls back to the
    cloud bound when a cloud key is configured, else the local bound (an
    unconfigured box is local-only). A long-running tool keeps the stream "alive"
    on the client by resetting the timer on each output chunk, so this only has
    to cover the gap between model tokens — not whole-tool duration.
    """
    # Imported lazily to keep this config module import-light and cycle-free.
    from shared.config import check_cloud_availability

    try:
        cfg = load_byom_config()
        target = cfg.chat_models.get("big") or cfg.chat_models.get("medium")
        if target is not None:
            return _WATCHDOG_LOCAL_MS if target.is_local else _WATCHDOG_CLOUD_MS
    except Exception as exc:  # noqa: BLE001 — a config read must never break submit
        logger.warning("stream_watchdog_ms: config read failed (%s); using fallback", exc)
    return _WATCHDOG_CLOUD_MS if check_cloud_availability() else _WATCHDOG_LOCAL_MS


def load_byom_config() -> BYOMConfig:
    """Read and validate byom_config.json. Returns safe defaults if missing or corrupt."""
    if not BYOM_CONFIG_PATH.exists():
        return BYOMConfig()
    try:
        return BYOMConfig.model_validate_json(
            BYOM_CONFIG_PATH.read_text(encoding="utf-8")
        )
    except Exception as exc:
        logger.warning("Invalid %s — falling back to defaults: %s", BYOM_CONFIG_PATH, exc)
        return BYOMConfig()


def save_byom_config(config: BYOMConfig) -> None:
    """Atomic + 0600 + UTF-8 write.

    Properties guaranteed by this writer:
    - Atomic: uses tempfile.mkstemp + os.replace so a power-loss mid-write
      leaves the old file intact (never a half-written JSON).
    - 0600: only the process owner can read the file (protects stored API keys).
    - UTF-8: explicit encoding prevents CP1252 corruption on Windows.
    - Safe cleanup: if the write itself fails, the temp file is removed without
      masking the original exception.
    """
    BYOM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump_json(indent=2)
    fd, tmp = tempfile.mkstemp(dir=BYOM_CONFIG_PATH.parent, prefix=".tmp_byom_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, BYOM_CONFIG_PATH)
        logger.info("BYOM config saved to %s", BYOM_CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
