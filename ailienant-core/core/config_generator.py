# ailienant-core/core/config_generator.py
#
# Phase 1.6.3/1.6.4 — Zero-touch LiteLLM config generator.
# Probes known local AI endpoints and checks cloud env keys to produce
# a config.yaml that the LiteLLM proxy can consume on startup.
#
# No project imports — this module is intentionally self-contained so it
# can be imported early or run as a standalone script.

import logging
import os
from typing import Any, Dict, List, Optional, TypedDict

import httpx
import yaml

logger = logging.getLogger("ConfigGenerator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLLAMA_API_BASE: str = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
LM_STUDIO_API_BASE: str = os.getenv("LM_STUDIO_API_BASE", "http://localhost:1234")
CONFIG_YAML_PATH: str = os.getenv("LITELLM_CONFIG_PATH", "config.yaml")
_PROBE_TIMEOUT: float = 2.0  # seconds — fast enough not to block startup

# Heuristic tier assignment based on model name substrings (longest match wins).
_SMALL_PATTERNS = ("phi3", "phi-3", "gemma2:2b", "qwen2.5:3b", "tinyllama", "smollm", "0.5b", "1b", "2b", "3b")
_BIG_PATTERNS = ("70b", "72b", "8x7b", "mixtral:8x", "deepseek-r1", "llama3.3", "qwen2.5:72b", "405b")

# Cloud provider definitions: (env_var, alias_tier, real_model_id, provider_name)
_CLOUD_PROVIDERS = [
    ("ANTHROPIC_API_KEY", "ailienant/big", "claude-3-5-sonnet-20241022", "anthropic"),
    ("OPENAI_API_KEY", "ailienant/big", "gpt-4o", "openai"),
    ("GOOGLE_API_KEY", "ailienant/big", "gemini/gemini-2.0-flash", "google"),
    ("DEEPSEEK_API_KEY", "ailienant/medium", "deepseek/deepseek-chat", "deepseek"),
    ("MISTRAL_API_KEY", "ailienant/small", "mistral/mistral-small-latest", "mistral"),
    ("DASHSCOPE_API_KEY", "ailienant/medium", "openai/qwen-max", "qwen"),
    ("MOONSHOT_API_KEY", "ailienant/medium", "openai/moonshot-v1-128k", "moonshot"),
    ("ZHIPU_API_KEY", "ailienant/big", "openai/glm-4-plus", "zhipu"),
]


# ---------------------------------------------------------------------------
# TypedDicts (no Pydantic — keeps this module dependency-free)
# ---------------------------------------------------------------------------


class DiscoveredModel(TypedDict):
    id: str        # LiteLLM alias, e.g. "ailienant/medium"
    name: str      # underlying model name, e.g. "llama3.1"
    provider: str  # "ollama" | "openai" | "anthropic" | etc.
    is_local: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_tier(model_name: str) -> str:
    """Return 'small' | 'medium' | 'big' based on model name patterns."""
    lower = model_name.lower()
    if any(p in lower for p in _BIG_PATTERNS):
        return "big"
    if any(p in lower for p in _SMALL_PATTERNS):
        return "small"
    return "medium"


async def _probe_ollama() -> Optional[List[str]]:
    """
    GET /api/tags from Ollama. Returns a list of model names or None if
    Ollama is unreachable within the probe timeout.
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{OLLAMA_API_BASE}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return None


async def _probe_lmstudio() -> Optional[List[str]]:
    """GET /v1/models from LM Studio (OpenAI-compatible). Returns model IDs or None."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{LM_STUDIO_API_BASE}/v1/models")
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", []) if m.get("id")]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def discover_models() -> List[DiscoveredModel]:
    """
    Probe local services and check cloud env keys.
    Returns a flat list of DiscoveredModel dicts — may contain multiple
    entries mapping to the same alias tier (e.g., two Ollama medium models).
    """
    models: List[DiscoveredModel] = []

    # 1. Ollama
    ollama_names = await _probe_ollama()
    if ollama_names:
        for name in ollama_names:
            tier = _infer_tier(name)
            models.append(DiscoveredModel(
                id=f"ailienant/{tier}",
                name=name,
                provider="ollama",
                is_local=True,
            ))
        logger.info("Ollama: discovered %d model(s) at %s", len(ollama_names), OLLAMA_API_BASE)
    else:
        logger.debug("Ollama not reachable at %s", OLLAMA_API_BASE)

    # 2. Cloud providers via environment keys
    for env_key, alias, model_name, provider in _CLOUD_PROVIDERS:
        if os.getenv(env_key):
            models.append(DiscoveredModel(
                id=alias,
                name=model_name,
                provider=provider,
                is_local=False,
            ))
            logger.info("Cloud provider detected: %s (%s)", provider, alias)

    if not models:
        logger.warning(
            "No local or cloud models detected. "
            "Start Ollama or set a cloud API key env var."
        )

    return models


async def write_config(output_path: str = CONFIG_YAML_PATH) -> Dict[str, Any]:
    """
    Discover available models and write a LiteLLM proxy config.yaml.

    Returns a summary dict:
        {"path": str, "ollama_models": List[str], "cloud_providers": List[str],
         "total_entries": int}
    """
    model_list = []
    ollama_names = await _probe_ollama()
    discovered_cloud: List[str] = []

    if ollama_names:
        for name in ollama_names:
            tier = _infer_tier(name)
            model_list.append({
                "model_name": f"ailienant/{tier}",
                "litellm_params": {
                    "model": f"ollama/{name}",
                    "api_base": OLLAMA_API_BASE,
                },
            })

    for env_key, alias, model_name, provider in _CLOUD_PROVIDERS:
        if os.getenv(env_key):
            model_list.append({
                "model_name": alias,
                "litellm_params": {
                    "model": model_name,
                    "api_key": f"os.environ/{env_key}",
                },
            })
            discovered_cloud.append(provider)

    # Fallback: at least one entry so LiteLLM starts without errors
    if not model_list:
        model_list.append({
            "model_name": "ailienant/medium",
            "litellm_params": {
                "model": "ollama/llama3.1",
                "api_base": OLLAMA_API_BASE,
            },
        })
        logger.warning("No models detected — wrote fallback config (ollama/llama3.1)")

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump({"model_list": model_list}, f, default_flow_style=False, allow_unicode=True)

    summary = {
        "path": output_path,
        "ollama_models": ollama_names or [],
        "cloud_providers": discovered_cloud,
        "total_entries": len(model_list),
    }
    logger.info("LiteLLM config written: %s", summary)
    return summary


def write_config_with_overrides(
    output_path: str,
    tier_overrides: Dict[str, Any],
) -> None:
    """Phase 7.9.B.2 — Write config.yaml synchronously with user-defined tier overrides.

    ``tier_overrides`` maps LiteLLM alias names to concrete litellm_params.model
    values, e.g. ``{"ailienant/small": "ollama/phi3", "ailienant/big": "gpt-4o"}``.
    Entries not in the override dict are written from the existing file unchanged,
    or omitted if no existing file is found (the auto-discovery flow handles that
    case at startup; this function is called from async context via to_thread).

    The write is atomic (mkstemp → os.replace) to prevent truncation on power
    loss.  config.yaml contains no secrets so 0600 is not required here, but
    the atomic guarantee still applies.
    """
    import tempfile as _tempfile

    # Read the current config.yaml as the base (best-effort; fall back to empty list).
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    except FileNotFoundError:
        existing = {}

    model_list: List[Dict[str, Any]] = list(existing.get("model_list", []))

    if tier_overrides:
        # Build an index of existing entries by model_name for fast lookup.
        by_alias: Dict[str, Any] = {entry["model_name"]: entry for entry in model_list}

        for alias, model_id in tier_overrides.items():
            if not model_id:
                continue  # skip empty overrides
            if alias in by_alias:
                by_alias[alias]["litellm_params"]["model"] = model_id
                # Remove api_base if switching from Ollama to a remote provider.
                if not model_id.startswith("ollama/"):
                    by_alias[alias]["litellm_params"].pop("api_base", None)
                elif "api_base" not in by_alias[alias]["litellm_params"]:
                    by_alias[alias]["litellm_params"]["api_base"] = OLLAMA_API_BASE
            else:
                # Alias not yet in the file — add a new entry.
                params: Dict[str, Any] = {"model": model_id}
                if model_id.startswith("ollama/"):
                    params["api_base"] = OLLAMA_API_BASE
                by_alias[alias] = {"model_name": alias, "litellm_params": params}

        model_list = list(by_alias.values())

    # Fallback: keep LiteLLM from failing on an empty model list.
    if not model_list:
        model_list.append({
            "model_name": "ailienant/medium",
            "litellm_params": {"model": "ollama/llama3.1", "api_base": OLLAMA_API_BASE},
        })

    content = yaml.dump(
        {"model_list": model_list}, default_flow_style=False, allow_unicode=True
    )

    # Atomic write: write to a temp file then os.replace to prevent partial writes.
    import pathlib as _pl
    parent = _pl.Path(output_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(dir=parent, prefix=".tmp_cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, output_path)
        logger.info("config.yaml written with %d overrides → %s", len(tier_overrides), output_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
