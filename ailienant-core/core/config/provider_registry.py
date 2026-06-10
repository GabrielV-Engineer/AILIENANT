"""core/config/provider_registry.py

Single source of truth for model-provider metadata. Every place that needs to
know how to route a model to litellm, where its endpoint lives, which env var
holds its key, or how to present it in the dashboard reads from here — so adding
a provider is a one-entry change instead of a six-site edit.

Routing of the model string to litellm is captured by ``model_style``:

  - ``"prefix"``      → ``f"{litellm_prefix}/{name}"`` with no api_base. litellm
                        owns the endpoint (Gemini, DeepSeek, Mistral).
  - ``"bare"``        → the model id passes through unchanged. Native providers
                        litellm recognises by name (OpenAI, Anthropic); OpenRouter
                        rides this style with a fixed api_base.
  - ``"openai_compat"`` → ``f"openai/{name}"`` with a custom api_base. Any
                        OpenAI-compatible server (the Chinese providers, vLLM,
                        LM Studio, generic custom).
  - ``"ollama_chat"`` → ``f"ollama_chat/{name}"`` (litellm ``/api/chat`` route).

``uses_api_base`` decides whether an api_base is sent at all; ``ensure_v1``
normalises a user-supplied local base URL to end in ``/v1`` (generic OpenAI-
compatible servers) — it is deliberately OFF for cloud providers whose base URL
is fixed and already correct (this is what keeps Google's ``/v1beta/openai/``
endpoint from being mangled into an invalid ``/v1`` suffix).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

ModelStyle = Literal["prefix", "bare", "openai_compat", "ollama_chat"]


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the system needs to know about one model provider."""

    id: str
    label: str
    model_style: ModelStyle
    is_local: bool
    needs_key: bool
    litellm_prefix: Optional[str] = None   # for model_style == "prefix"
    uses_api_base: bool = False            # whether to send an api_base at all
    default_base_url: Optional[str] = None  # chat base (openai_compat) / local default / test host
    ensure_v1: bool = False                # append /v1 to a user-supplied local base
    test_models_url: Optional[str] = None  # OpenAI-compatible /models probe for key validation
    env_key: Optional[str] = None          # env var fallback for the api key
    key_hint: str = ""                     # placeholder shown in the UI
    help_url: str = ""                     # "get your key" link
    suggested_models: List[str] = field(default_factory=list)  # full "{id}/{model}" ids
    embedding_model: Optional[str] = None  # litellm embedding id if the provider offers one

    @property
    def hides_base_url(self) -> bool:
        """Cloud providers have a known, fixed endpoint — the UI hides the field.
        Local engines need the user to point at their own server."""
        return not self.is_local


# ---------------------------------------------------------------------------
# The registry — the only place provider knowledge lives.
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: Dict[str, ProviderSpec] = {
    # ── Local engines ──────────────────────────────────────────────────────
    "ollama": ProviderSpec(
        id="ollama", label="Ollama", model_style="ollama_chat",
        is_local=True, needs_key=False, uses_api_base=True,
        default_base_url="http://localhost:11434",
    ),
    "lmstudio": ProviderSpec(
        id="lmstudio", label="LM Studio", model_style="openai_compat",
        is_local=True, needs_key=False, uses_api_base=True, ensure_v1=True,
        default_base_url="http://localhost:1234",
    ),
    "vllm": ProviderSpec(
        id="vllm", label="vLLM", model_style="openai_compat",
        is_local=True, needs_key=False, uses_api_base=True, ensure_v1=True,
        default_base_url="http://localhost:8000",
    ),
    "custom": ProviderSpec(
        id="custom", label="Custom (OpenAI-compatible)", model_style="openai_compat",
        is_local=True, needs_key=False, uses_api_base=True, ensure_v1=True,
        default_base_url="",
    ),
    # ── Western cloud (existing) ───────────────────────────────────────────
    "openai": ProviderSpec(
        id="openai", label="OpenAI", model_style="bare",
        is_local=False, needs_key=True, uses_api_base=False,
        test_models_url="https://api.openai.com/v1/models",
        env_key="OPENAI_API_KEY", key_hint="sk-…",
        help_url="https://platform.openai.com/api-keys",
        suggested_models=["openai/gpt-4o", "openai/gpt-4o-mini"],
        embedding_model="text-embedding-3-small",
    ),
    "anthropic": ProviderSpec(
        id="anthropic", label="Anthropic", model_style="bare",
        is_local=False, needs_key=True, uses_api_base=False,
        env_key="ANTHROPIC_API_KEY", key_hint="sk-ant-…",
        help_url="https://console.anthropic.com/settings/keys",
        suggested_models=[
            "anthropic/claude-3-5-sonnet-20241022",
            "anthropic/claude-3-5-haiku-20241022",
        ],
    ),
    "openrouter": ProviderSpec(
        id="openrouter", label="OpenRouter", model_style="bare",
        is_local=False, needs_key=True, uses_api_base=True,
        default_base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY", key_hint="sk-or-…",
        help_url="https://openrouter.ai/keys",
    ),
    # ── Google (native — fixes the /v1 mangling) ───────────────────────────
    "google": ProviderSpec(
        id="google", label="Google Gemini", model_style="prefix",
        litellm_prefix="gemini", is_local=False, needs_key=True, uses_api_base=False,
        test_models_url="https://generativelanguage.googleapis.com/v1beta/openai/models",
        env_key="GOOGLE_API_KEY", key_hint="AIza…",
        help_url="https://aistudio.google.com/apikey",
        suggested_models=[
            "google/gemini-2.0-flash",
            "google/gemini-2.5-flash",
            "google/gemini-1.5-pro",
        ],
        embedding_model="gemini/text-embedding-004",
    ),
    # ── DeepSeek (native) ──────────────────────────────────────────────────
    "deepseek": ProviderSpec(
        id="deepseek", label="DeepSeek", model_style="prefix",
        litellm_prefix="deepseek", is_local=False, needs_key=True, uses_api_base=False,
        test_models_url="https://api.deepseek.com/v1/models",
        env_key="DEEPSEEK_API_KEY", key_hint="sk-…",
        help_url="https://platform.deepseek.com/api_keys",
        suggested_models=["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"],
    ),
    # ── Mistral (native) ───────────────────────────────────────────────────
    "mistral": ProviderSpec(
        id="mistral", label="Mistral", model_style="prefix",
        litellm_prefix="mistral", is_local=False, needs_key=True, uses_api_base=False,
        test_models_url="https://api.mistral.ai/v1/models",
        env_key="MISTRAL_API_KEY", key_hint="…",
        help_url="https://console.mistral.ai/api-keys",
        suggested_models=[
            "mistral/mistral-small-latest",
            "mistral/codestral-latest",
            "mistral/mistral-large-latest",
        ],
    ),
    # ── Alibaba Qwen / DashScope (OpenAI-compatible) ───────────────────────
    "qwen": ProviderSpec(
        id="qwen", label="Alibaba Qwen", model_style="openai_compat",
        is_local=False, needs_key=True, uses_api_base=True,
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        test_models_url="https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        env_key="DASHSCOPE_API_KEY", key_hint="sk-…",
        help_url="https://dashscope.console.aliyun.com/apiKey",
        suggested_models=["qwen/qwen-max", "qwen/qwen-plus", "qwen/qwen-coder-plus"],
    ),
    # ── Moonshot / Kimi (OpenAI-compatible) ────────────────────────────────
    "moonshot": ProviderSpec(
        id="moonshot", label="Moonshot (Kimi)", model_style="openai_compat",
        is_local=False, needs_key=True, uses_api_base=True,
        default_base_url="https://api.moonshot.cn/v1",
        test_models_url="https://api.moonshot.cn/v1/models",
        env_key="MOONSHOT_API_KEY", key_hint="sk-…",
        help_url="https://platform.moonshot.cn/console/api-keys",
        suggested_models=["moonshot/moonshot-v1-128k", "moonshot/moonshot-v1-32k"],
    ),
    # ── Zhipu / GLM (OpenAI-compatible) ────────────────────────────────────
    "zhipu": ProviderSpec(
        id="zhipu", label="Zhipu (GLM)", model_style="openai_compat",
        is_local=False, needs_key=True, uses_api_base=True,
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        test_models_url="https://open.bigmodel.cn/api/paas/v4/models",
        env_key="ZHIPU_API_KEY", key_hint="…",
        help_url="https://open.bigmodel.cn/usercenter/apikeys",
        suggested_models=["zhipu/glm-4-plus", "zhipu/glm-4-flash"],
    ),
}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

PROVIDER_IDS: List[str] = list(PROVIDER_REGISTRY.keys())
CLOUD_PROVIDER_IDS: List[str] = [p.id for p in PROVIDER_REGISTRY.values() if not p.is_local]
# Env-var names every cloud provider reads its key from (drives cloud-availability checks).
PROVIDER_ENV_KEYS: List[str] = [
    p.env_key for p in PROVIDER_REGISTRY.values() if p.env_key is not None
]


def get_provider(provider_id: str) -> Optional[ProviderSpec]:
    """Return the spec for ``provider_id`` or ``None`` when unknown."""
    return PROVIDER_REGISTRY.get(provider_id)


def normalize_model_string(model_id: str, spec: ProviderSpec) -> str:
    """Map a stored tier model id to its litellm-routable string per the spec.

    The stored id may carry our provider prefix (``"google/gemini-2.0-flash"``);
    the bare name is whatever follows the first ``/``.
    """
    name = model_id.split("/", 1)[1] if "/" in model_id else model_id
    if spec.model_style == "ollama_chat":
        return f"ollama_chat/{name}"
    if spec.model_style == "openai_compat":
        return f"openai/{name}"
    if spec.model_style == "prefix" and spec.litellm_prefix:
        return f"{spec.litellm_prefix}/{name}"
    # "bare": pass the id through unchanged (OpenAI / Anthropic / OpenRouter).
    return model_id
