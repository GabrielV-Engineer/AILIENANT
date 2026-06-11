"""Provider registry — routing + connection resolution + the Google /v1 fix.

Certifies that the single-source-of-truth provider registry routes each model
provider to the correct litellm string + api_base + key, that native cloud
providers (Gemini/DeepSeek/Mistral) carry NO api_base (so a fixed cloud endpoint
is never mangled with an appended `/v1`), and that `GET /providers` exposes the
registry without leaking secrets.
"""
from __future__ import annotations

import os
from typing import List

from core.config.byom_config import EndpointConfig
from core.config.provider_registry import (
    CLOUD_PROVIDER_IDS,
    PROVIDER_REGISTRY,
    get_provider,
    normalize_model_string,
)
from api.byom import (
    _build_chat_target,
    _connection_for_provider,
    _normalize_chat_model,
)


def _ep(provider: str, key: str = "", url: str = "") -> List[EndpointConfig]:
    return [EndpointConfig(id="e1", name="t", url=url, api_key=key, provider=provider)]  # type: ignore[arg-type]


# ── Google — the headline bug: native routing, NO /v1 appended ────────────────


def test_google_native_routing_no_v1_mangling() -> None:
    target = _build_chat_target("google/gemini-2.0-flash", _ep("google", key="AIzaTEST"))
    assert target.model == "gemini/gemini-2.0-flash"   # litellm-native prefix
    assert target.api_base is None                     # litellm owns the endpoint
    assert "/v1" not in (target.api_base or "")        # the regression guard
    assert target.api_key == "AIzaTEST"
    assert target.is_local is False


def test_google_key_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "env-google-key")
    api_base, api_key, is_local = _connection_for_provider("google", [])
    assert api_base is None
    assert api_key == "env-google-key"
    assert is_local is False


# ── Native cloud providers (prefix style) ─────────────────────────────────────


def test_deepseek_and_mistral_native_prefix() -> None:
    ds = _build_chat_target("deepseek/deepseek-chat", _ep("deepseek", key="sk-ds"))
    assert ds.model == "deepseek/deepseek-chat"
    assert ds.api_base is None

    ms = _build_chat_target("mistral/codestral-latest", _ep("mistral", key="m-key"))
    assert ms.model == "mistral/codestral-latest"
    assert ms.api_base is None


# ── OpenAI-compatible Chinese providers (openai/ + base) ──────────────────────


def test_qwen_openai_compat_routing() -> None:
    t = _build_chat_target("qwen/qwen-max", _ep("qwen", key="sk-qwen"))
    assert t.model == "openai/qwen-max"
    assert t.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert t.api_key == "sk-qwen"
    assert t.is_local is False


def test_moonshot_and_zhipu_openai_compat() -> None:
    mo = _build_chat_target("moonshot/moonshot-v1-128k", _ep("moonshot", key="k"))
    assert mo.model == "openai/moonshot-v1-128k"
    assert mo.api_base == "https://api.moonshot.cn/v1"

    zh = _build_chat_target("zhipu/glm-4-plus", _ep("zhipu", key="k"))
    assert zh.model == "openai/glm-4-plus"
    assert zh.api_base == "https://open.bigmodel.cn/api/paas/v4"


# ── Existing providers preserved (no behavioral regression) ───────────────────


def test_openai_anthropic_bare_passthrough() -> None:
    oa = _build_chat_target("openai/gpt-4o", _ep("openai", key="sk-oa"))
    assert oa.model == "openai/gpt-4o"   # bare passthrough
    assert oa.api_base is None

    an = _build_chat_target("anthropic/claude-3-5-sonnet-20241022", _ep("anthropic", key="sk-an"))
    assert an.api_base is None


def test_openrouter_native_routing() -> None:
    # OpenRouter now routes natively (openrouter/<model>) with no api_base —
    # litellm owns the endpoint and reads OPENROUTER_API_KEY.
    api_base, _key, is_local = _connection_for_provider("openrouter", _ep("openrouter", key="sk-or"))
    assert api_base is None
    assert is_local is False
    t = _build_chat_target("openrouter/meta-llama/llama-3.3-70b-instruct:free", _ep("openrouter", key="sk-or"))
    assert t.model == "openrouter/meta-llama/llama-3.3-70b-instruct:free"
    assert t.api_base is None


def test_local_custom_still_appends_v1() -> None:
    # Generic OpenAI-compatible local servers keep the /v1 normalization.
    api_base, _key, is_local = _connection_for_provider(
        "custom", _ep("custom", url="http://localhost:9000")
    )
    assert api_base == "http://localhost:9000/v1"
    assert is_local is True


def test_ollama_chat_route() -> None:
    assert _normalize_chat_model("ollama/llama3.1", "ollama") == "ollama_chat/llama3.1"


# ── Registry invariants ───────────────────────────────────────────────────────


def test_six_new_providers_present_and_cloud() -> None:
    for pid in ("google", "deepseek", "mistral", "qwen", "moonshot", "zhipu"):
        spec = get_provider(pid)
        assert spec is not None, pid
        assert spec.is_local is False
        assert spec.needs_key is True
        assert spec.hides_base_url is True   # cloud → base URL hidden in UI
        assert pid in CLOUD_PROVIDER_IDS


def test_normalize_model_string_matrix() -> None:
    assert normalize_model_string("google/gemini-2.0-flash", PROVIDER_REGISTRY["google"]) == "gemini/gemini-2.0-flash"
    assert normalize_model_string("qwen/qwen-max", PROVIDER_REGISTRY["qwen"]) == "openai/qwen-max"
    assert normalize_model_string("gpt-4o", PROVIDER_REGISTRY["openai"]) == "gpt-4o"


# ── GET /providers projection carries no secrets ──────────────────────────────


def test_get_providers_has_no_secrets() -> None:
    import asyncio

    from api.byom import get_providers

    specs = asyncio.run(get_providers())
    ids = {s.id for s in specs}
    assert {"google", "deepseek", "mistral", "qwen", "moonshot", "zhipu"} <= ids
    # The projection model only carries env-key *names*, never values; assert the
    # field set is exactly the safe projection (no `api_key`/secret field exists).
    fields = set(specs[0].model_dump().keys())
    assert "api_key" not in fields
    assert "env_key" in fields  # name only
