# shared/config.py

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Application home — the stable per-user root for all global runtime stores.
# Mirrors how a CLI tool keeps its state under a dotfolder in the user's home,
# so the stores no longer depend on the process working directory (launching
# from a different CWD would otherwise orphan the catalog / vector index).
# Created at import so the very first store connection finds the directory.
# ---------------------------------------------------------------------------
AILIENANT_HOME: Path = Path.home() / ".ailienant"
AILIENANT_HOME.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# LiteLLM Proxy (Phase 1.6) — all agent LLM calls route through this endpoint
# ---------------------------------------------------------------------------
LITELLM_PROXY_BASE_URL: str = os.getenv("LITELLM_PROXY_BASE_URL", "http://localhost:4000")
LITELLM_PROXY_API_KEY: str = os.getenv("LITELLM_PROXY_API_KEY", "sk-ailienant-local")

# ---------------------------------------------------------------------------
# Local engine base URLs (Phase 7.9.B.12) — shared by config_generator,
# the embedding resolver and the indexer preflight so all probes agree.
# ---------------------------------------------------------------------------
OLLAMA_API_BASE: str = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
LM_STUDIO_API_BASE: str = os.getenv("LM_STUDIO_API_BASE", "http://localhost:1234")

# Ailienant model alias tiers — mapped to real models inside LiteLLM proxy config.yaml.
# Override via env to switch providers without touching code (Phase 1.6.2).
MODEL_SMALL: str = os.getenv("AILIENANT_MODEL_SMALL", "ailienant/small")
MODEL_MEDIUM: str = os.getenv("AILIENANT_MODEL_MEDIUM", "ailienant/medium")
MODEL_BIG: str = os.getenv("AILIENANT_MODEL_BIG", "ailienant/big")


def get_litellm_config() -> dict[str, str]:
    """Base kwargs injected into every litellm.completion() / acompletion() call."""
    return {
        "base_url": LITELLM_PROXY_BASE_URL,
        "api_key": LITELLM_PROXY_API_KEY,
    }


# ---------------------------------------------------------------------------
# VRAM gating thresholds (effective GB) — configurable, not frozen constants.
# The hardware detector reads the swarm gates from here so an operator can tune
# the local/cloud frontier per machine without a code change. The cloud floor is
# the point below which the routing engine bypasses local inference to the cloud
# (graceful degradation): below it, even a small local model cannot run safely.
# A malformed override degrades to the documented default rather than raising at
# import time, since this is a foundational module on every startup path.
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


VRAM_MICRO_SWARM_GB: float = _env_float("AILIENANT_VRAM_MICRO_SWARM_GB", 4.0)
VRAM_FULL_SWARM_GB: float = _env_float("AILIENANT_VRAM_FULL_SWARM_GB", 12.0)
VRAM_CLOUD_FLOOR_GB: float = _env_float("AILIENANT_VRAM_CLOUD_FLOOR_GB", 4.0)


# ---------------------------------------------------------------------------
# Cloud availability detection (used by Phase 2 routing engine)
# ---------------------------------------------------------------------------
# Mirrors the cloud env keys declared in core/config/provider_registry.py
# (kept as a flat list here to avoid an import cycle in this foundational module).
CLOUD_PROVIDER_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "AILIENANT_CUSTOM_CLOUD_ENDPOINT",
]


def check_cloud_availability() -> bool:
    """Returns True if at least one cloud provider key is configured."""
    return any(os.getenv(key) for key in CLOUD_PROVIDER_KEYS)


# ---------------------------------------------------------------------------
# Catalog DB — global command/graph store, separate from LangGraph's
# alienant_memory.sqlite. Per-project rows are isolated by a project_id column;
# the file itself is global (skills / MCP servers / hooks are shared across
# projects), so it lives in the application home.
# ---------------------------------------------------------------------------
DB_CATALOG_PATH: str = os.getenv("AILIENANT_CATALOG_DB", str(AILIENANT_HOME / "catalog.sqlite"))

# Global LanceDB store — home of the cross-project tables (product-doc index and
# trajectory memory). The per-project GraphRAG store (workspace_embeddings) is
# resolved separately by core.storage_paths so each project gets its own index.
LANCEDB_PATH: str = os.getenv("AILIENANT_LANCEDB_PATH", str(AILIENANT_HOME / "lancedb"))

# MCTS episodic audit DB — global, retention-pruned by the janitor.
MCTS_DB_PATH: str = os.getenv("AILIENANT_MCTS_DB", str(AILIENANT_HOME / "mcts.sqlite"))
# Phase 7.9.B.12 — advanced override ONLY. When unset, the embedding backend is
# resolved per-provider from the active BYOM preset (core/config/embedding_resolver.py).
# Setting this env var forces a fixed embedding model regardless of the preset.
MODEL_EMBEDDING: str = os.getenv("AILIENANT_MODEL_EMBEDDING", "ailienant/embedding")
MINI_JUDGE_MODEL: str = os.getenv("AILIENANT_MINI_JUDGE_MODEL", MODEL_SMALL)

# Phase 5.2 — MCP transport URI (None → local-only fallback, no MCP session).
# Format expected: "stdio:///absolute/path/to/server[?arg=...]" (only stdio
# supported in 5.2; websocket/http transports deferred).
AILIENANT_MCP_SERVER_URI: str | None = os.getenv("AILIENANT_MCP_SERVER_URI") or None
