# ailienant-core/tests/test_llm_gateway_timeout.py
"""Phase 7.9.B.19 — local BYOM models receive the extended 300 s timeout."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.llm_gateway import LLMGateway, _LOCAL_LLM_TIMEOUT_S


def _make_target(is_local: bool, model: str = "ollama_chat/phi4") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.api_base = "http://localhost:11434"
    t.api_key = None
    t.is_local = is_local
    return t


def _mock_response() -> MagicMock:
    resp = MagicMock()
    resp.usage = None
    resp.choices = [MagicMock()]
    return resp


@pytest.mark.anyio
async def test_ainvoke_local_byom_uses_extended_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    assert mock_litellm.await_args.kwargs.get("timeout") == _LOCAL_LLM_TIMEOUT_S


@pytest.mark.anyio
async def test_ainvoke_cloud_byom_keeps_caller_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=False, model="claude-haiku-4-5-20251001")), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
            timeout=60.0,
        )
    assert mock_litellm.await_args.kwargs.get("timeout") == 60.0


@pytest.mark.anyio
async def test_acomplete_byom_local_uses_extended_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.acomplete_byom(
            messages=[{"role": "user", "content": "hi"}],
        )
    assert mock_litellm.await_args.kwargs.get("timeout") == _LOCAL_LLM_TIMEOUT_S
