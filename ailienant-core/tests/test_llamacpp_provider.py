# ailienant-core/tests/test_llamacpp_provider.py
"""llama.cpp (llama-server) as a first-class BYOM provider.

Covers registry routing, registry-derived endpoint validation, per-endpoint
routing for several servers of one provider, the `/props` runtime probe, the
dedicated embedding server (probed dimension), and local-aware built-in presets.
All network I/O is sealed behind an httpx MockTransport.
"""
from __future__ import annotations

import json
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from api import byom
from core.config import model_resolver
from core.config.byom_config import EmbeddingTarget, EndpointConfig, ModelTarget
from core.config.provider_registry import get_provider, normalize_model_string
from shared.config import LLAMA_CPP_API_BASE, LLAMA_CPP_EMBED_API_BASE

pytestmark = pytest.mark.unit

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[..., Any]:
    transport = httpx.MockTransport(handler)
    return lambda **_: _RealAsyncClient(transport=transport)


def _ep(ep_id: str, url: str, provider: str = "llamacpp") -> EndpointConfig:
    return EndpointConfig(id=ep_id, name=ep_id, url=url, provider=provider)


def _llama_target(model: str = "openai/qwen-coder-7b", base: str = "http://localhost:8080/v1") -> ModelTarget:
    return ModelTarget(model=model, provider="llamacpp", api_base=base, api_key="sk-noauth", is_local=True)


# ── registry + contract ─────────────────────────────────────────────────────


def test_registry_routes_llamacpp_through_openai_compat() -> None:
    spec = get_provider("llamacpp")
    assert spec is not None
    assert spec.is_local and not spec.needs_key and spec.ensure_v1
    assert spec.default_base_url == LLAMA_CPP_API_BASE
    assert normalize_model_string("llamacpp/qwen-coder-7b", spec) == "openai/qwen-coder-7b"


def test_endpoint_provider_is_validated_against_registry() -> None:
    assert _ep("a", "http://x").provider == "llamacpp"
    with pytest.raises(ValidationError):
        EndpointConfig(id="b", name="b", url="http://x", provider="not-a-provider")


def test_local_model_ids_always_carry_their_provider_prefix() -> None:
    # A local GGUF named like a cloud provider must stay attributed to the local engine.
    assert byom._canonical_model_id("llamacpp", "qwen/qwen3-8b") == "llamacpp/qwen/qwen3-8b"
    assert byom._canonical_model_id("llamacpp", "qwen-coder-7b") == "llamacpp/qwen-coder-7b"


def test_strip_v1_prevents_doubled_suffix() -> None:
    assert byom._strip_v1("http://localhost:8080/v1/") == "http://localhost:8080"
    assert byom._strip_v1("http://localhost:8080") == "http://localhost:8080"


# ── per-endpoint routing ────────────────────────────────────────────────────


def test_each_model_routes_to_the_server_that_lists_it() -> None:
    endpoints = [_ep("small-srv", "http://localhost:8080"), _ep("big-srv", "http://localhost:8090")]
    cache = {"small-srv": ["llamacpp/qwen-3b"], "big-srv": ["llamacpp/qwen-32b"]}

    small = byom._build_chat_target("llamacpp/qwen-3b", endpoints, cache)
    big = byom._build_chat_target("llamacpp/qwen-32b", endpoints, cache)

    assert small.api_base == "http://localhost:8080/v1"
    assert big.api_base == "http://localhost:8090/v1"
    assert big.model == "openai/qwen-32b"
    assert big.is_local and big.api_key == "sk-noauth"


def test_router_mode_serves_several_models_from_one_endpoint() -> None:
    endpoints = [_ep("router", "http://localhost:8080/v1")]
    cache = {"router": ["llamacpp/qwen-3b", "llamacpp/qwen-32b"]}
    a = byom._build_chat_target("llamacpp/qwen-3b", endpoints, cache)
    b = byom._build_chat_target("llamacpp/qwen-32b", endpoints, cache)
    assert a.api_base == b.api_base == "http://localhost:8080/v1"
    assert (a.model, b.model) == ("openai/qwen-3b", "openai/qwen-32b")


def test_uncached_model_falls_back_to_first_endpoint() -> None:
    endpoints = [_ep("first", "http://localhost:8080"), _ep("second", "http://localhost:8090")]
    t = byom._build_chat_target("llamacpp/unknown", endpoints, {})
    assert t.api_base == "http://localhost:8080/v1"


# ── runtime capability probe ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_props_probe_reads_served_context_window() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})

    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(handler)):
        caps = await model_resolver.probe_runtime_capabilities(_llama_target())
    model_resolver.refresh()

    assert caps.context_length == 16384
    assert caps.supports_thinking is False
    # Native endpoint lives at the server root, with the model for router mode.
    assert seen[0].path == "/props"
    assert seen[0].params["model"] == "qwen-coder-7b"


@pytest.mark.anyio
async def test_props_probe_failure_degrades_to_unknown() -> None:
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(lambda _r: httpx.Response(500))):
        caps = await model_resolver.probe_runtime_capabilities(_llama_target())
    model_resolver.refresh()
    assert caps.context_length is None


@pytest.mark.anyio
async def test_llamacpp_never_gets_a_per_call_num_ctx() -> None:
    assert await model_resolver.resolve_num_ctx(_llama_target(), min_required=4096) is None


# ── embeddings ──────────────────────────────────────────────────────────────


def test_embedding_target_uses_dedicated_embedding_server() -> None:
    t = byom._build_embedding_target("llamacpp", [_ep("chat", "http://localhost:8080")], False)
    assert t.provider == "llamacpp" and t.is_local
    assert t.api_base == byom._ensure_v1(LLAMA_CPP_EMBED_API_BASE)
    assert t.model.startswith("openai/")


@pytest.mark.anyio
async def test_embedding_dimension_is_probed_from_the_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        assert json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1024}]})

    base = byom._build_embedding_target("llamacpp", [], False)
    with patch("httpx.AsyncClient", _client_factory(handler)), \
            patch.object(byom, "LLAMA_CPP_EMBED_DIM", ""):
        probed = await byom._probe_embedding_dim(base)
    assert probed.dim == 1024


@pytest.mark.anyio
async def test_embedding_dimension_keeps_fallback_when_server_down() -> None:
    base = byom._build_embedding_target("llamacpp", [], False)
    with patch("httpx.AsyncClient", _client_factory(lambda _r: httpx.Response(503))), \
            patch.object(byom, "LLAMA_CPP_EMBED_DIM", ""):
        probed = await byom._probe_embedding_dim(base)
    assert probed.dim == base.dim


@pytest.mark.anyio
async def test_embedding_probe_ignores_other_providers() -> None:
    other = EmbeddingTarget(model="ollama/nomic-embed-text", provider="ollama", api_base="http://x")
    assert await byom._probe_embedding_dim(other) is other


# ── built-in presets ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_builtin_presets_treat_llamacpp_models_as_local() -> None:
    cache = {"llama-ep": ["llamacpp/qwen-coder-7b"], "cloud-ep": ["google/gemini-2.0-flash"]}
    with patch.object(byom, "discover_models", AsyncMock(return_value=[])):
        presets, discovered = await byom._build_builtin_presets(cache)
    by_id = {p.id: p for p in presets}

    assert set(by_id["builtin-local"].tiers.values()) == {"llamacpp/qwen-coder-7b"}
    assert by_id["builtin-hybrid"].tiers["medium"] == "llamacpp/qwen-coder-7b"
    assert by_id["builtin-hybrid"].tiers["big"] == "google/gemini-2.0-flash"
    assert set(by_id["builtin-cloud"].tiers.values()) == {"google/gemini-2.0-flash"}
    assert {d.id for d in discovered} == set(cache["llama-ep"] + cache["cloud-ep"])
