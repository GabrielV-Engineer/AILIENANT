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


def get_litellm_config() -> dict:
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
