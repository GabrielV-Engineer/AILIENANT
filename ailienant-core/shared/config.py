# shared/config.py

import os

# ---------------------------------------------------------------------------
# LiteLLM Proxy (Phase 1.6) — all agent LLM calls route through this endpoint
# ---------------------------------------------------------------------------
LITELLM_PROXY_BASE_URL: str = os.getenv("LITELLM_PROXY_BASE_URL", "http://localhost:4000")
LITELLM_PROXY_API_KEY: str = os.getenv("LITELLM_PROXY_API_KEY", "sk-ailienant-local")

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
# Cloud availability detection (used by Phase 2 routing engine)
# ---------------------------------------------------------------------------
CLOUD_PROVIDER_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "AILIENANT_CUSTOM_CLOUD_ENDPOINT",
]


def check_cloud_availability() -> bool:
    """Returns True if at least one cloud provider key is configured."""
    return any(os.getenv(key) for key in CLOUD_PROVIDER_KEYS)


# ---------------------------------------------------------------------------
# Catalog DB (Phase 1.x.1) — separate from LangGraph's alienant_memory.sqlite
# ---------------------------------------------------------------------------
DB_CATALOG_PATH: str = os.getenv("AILIENANT_CATALOG_DB", "ailienant_catalog.sqlite")

# Trajectory Memory (Phase 3.0.1) — LanceDB store and embedding model alias.
LANCEDB_PATH: str = os.getenv("AILIENANT_LANCEDB_PATH", "ailienant_lancedb")
MODEL_EMBEDDING: str = os.getenv("AILIENANT_MODEL_EMBEDDING", "ailienant/embedding")
MINI_JUDGE_MODEL: str = os.getenv("AILIENANT_MINI_JUDGE_MODEL", MODEL_SMALL)

# Phase 5.2 — MCP transport URI (None → local-only fallback, no MCP session).
# Format expected: "stdio:///absolute/path/to/server[?arg=...]" (only stdio
# supported in 5.2; websocket/http transports deferred).
AILIENANT_MCP_SERVER_URI: str | None = os.getenv("AILIENANT_MCP_SERVER_URI") or None
