# ailienant-core/tests/test_phase8_25_checkpoint_gate.py
"""Checkpoint gate: llama.cpp as a first-class BYOM provider (test-only).

Rows assert the cross-cutting invariants — single sources of truth and wiring —
that the focused suite (`test_llamacpp_provider.py`) does not.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import main
from api import byom
from core import config_generator
from core.config.byom_config import EndpointConfig
from core.config.provider_registry import PROVIDER_REGISTRY
from shared import config as shared_config

pytestmark = pytest.mark.unit


def test_g1_every_registry_provider_is_a_valid_endpoint_provider() -> None:
    # EndpointConfig derives its accepted ids from the registry: no second list.
    for provider_id in PROVIDER_REGISTRY:
        assert EndpointConfig(id="e", name="e", url="", provider=provider_id).provider == provider_id


def test_g2_model_discovery_locality_comes_from_the_registry() -> None:
    for provider_id, spec in PROVIDER_REGISTRY.items():
        assert main._is_local_litellm_prefix(provider_id) is spec.is_local
    assert main._is_local_litellm_prefix("ollama_chat") is True
    assert main._is_local_litellm_prefix("cloud") is False


def test_g3_engine_base_urls_have_one_source() -> None:
    assert config_generator.OLLAMA_API_BASE is shared_config.OLLAMA_API_BASE
    assert config_generator.LM_STUDIO_API_BASE is shared_config.LM_STUDIO_API_BASE
    assert PROVIDER_REGISTRY["llamacpp"].default_base_url == shared_config.LLAMA_CPP_API_BASE


@pytest.mark.anyio
async def test_g4_engine_status_reports_both_llamacpp_servers() -> None:
    with patch.object(byom, "_probe_ollama", AsyncMock(return_value=None)), \
            patch.object(byom, "_probe_lmstudio", AsyncMock(return_value=None)), \
            patch.object(byom, "_probe_llamacpp", AsyncMock(return_value=["qwen-coder-7b"])), \
            patch.object(byom, "_probe_llamacpp_embed", AsyncMock(return_value=None)):
        engines = {e.id: e for e in await byom.get_engine_status()}
    assert engines["llamacpp"].running and engines["llamacpp"].models == ["qwen-coder-7b"]
    assert engines["llamacpp-embed"].running is False
    assert engines["llamacpp-embed"].url == shared_config.LLAMA_CPP_EMBED_API_BASE


def test_g5_preset_activation_routes_chat_through_the_model_cache() -> None:
    # put_config / ping pass the model cache, so a second server is reachable.
    endpoints = [
        EndpointConfig(id="a", name="a", url="http://localhost:8080", provider="llamacpp"),
        EndpointConfig(id="b", name="b", url="http://localhost:8090", provider="llamacpp"),
    ]
    target = byom._build_chat_target("llamacpp/big", endpoints, {"b": ["llamacpp/big"]})
    assert target.api_base == "http://localhost:8090/v1"
