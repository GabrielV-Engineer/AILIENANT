# ailienant-core/tests/test_llm_gateway_num_ctx.py
"""Local Ollama calls request an explicit, real-derived `num_ctx` instead of
silently inheriting Ollama's own 4096-token default (13.1.3).

This is the direct physical mechanism behind a live planner failure: a real
prompt plus its requested completion exceeded the unconfigured 4096-token
window, Ollama context-shifted the prompt to make room, and the model drifted
off the JSON contract it could no longer fully see. These tests pin that every
BYOM call site (`ainvoke`, `acomplete_byom`, `astream_byom`,
`astream_byom_thinking`) requests `num_ctx` for a local Ollama target, omits it
for any other provider (the parameter does not exist there), and never lets a
resolution fault reach the caller.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config.model_resolver import RuntimeCapabilities
from tools.llm_gateway import LLMGateway

pytestmark = pytest.mark.anyio


def _ollama_target(model: str = "ollama_chat/gemma4:e4b") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.provider = "ollama"
    t.api_base = "http://localhost:11434"
    t.api_key = None
    t.is_local = True
    return t


def _cloud_target(model: str = "gpt-4o") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.provider = "openai"
    t.api_base = None
    t.api_key = "sk-x"
    t.is_local = False
    return t


def _mock_response() -> MagicMock:
    resp = MagicMock()
    resp.usage = None
    resp.choices = [MagicMock()]
    return resp


# ─── ainvoke ────────────────────────────────────────────────────────────────


async def test_ainvoke_local_ollama_requests_num_ctx() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_ollama_target()), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
            max_tokens=2000,
        )
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("num_ctx") is not None
    assert mock_litellm.await_args.kwargs["num_ctx"] >= 4096


async def test_ainvoke_cloud_target_never_gets_num_ctx() -> None:
    """The parameter does not exist for non-Ollama providers — sending it would
    be a wire-protocol error, not a harmless no-op."""
    with patch("core.config.model_resolver.get_chat_target", return_value=_cloud_target()), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    assert mock_litellm.await_args is not None
    assert "num_ctx" not in mock_litellm.await_args.kwargs


async def test_ainvoke_local_target_omits_num_ctx_when_the_probe_cannot_resolve_it() -> None:
    """A probe fault (unreachable Ollama, unknown model) must degrade to
    omitting the parameter — never a guessed value, never a blocked call."""
    with patch("core.config.model_resolver.get_chat_target", return_value=_ollama_target()), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    assert mock_litellm.await_args is not None
    assert "num_ctx" not in mock_litellm.await_args.kwargs


# ─── acomplete_byom / astream_byom / astream_byom_thinking ─────────────────


async def test_acomplete_byom_local_ollama_requests_num_ctx() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_ollama_target()), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("num_ctx") is not None


async def test_astream_byom_local_ollama_requests_num_ctx() -> None:
    async def _fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=MagicMock(content="hi"))]
        chunk.usage = None
        yield chunk

    with patch("core.config.model_resolver.get_chat_target", return_value=_ollama_target()), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_fake_stream())) as mock_litellm:
        async for _ in LLMGateway.astream_byom(messages=[{"role": "user", "content": "hi"}]):
            pass
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("num_ctx") is not None


async def test_astream_byom_thinking_reserves_room_for_the_thinking_budget() -> None:
    """A native reasoning turn generates thinking tokens ON TOP OF the answer —
    num_ctx must be sized off max_tokens + thinking_budget_tokens, not
    max_tokens alone, or this reproduces the exact bug for the one case native
    thinking is genuinely used."""
    async def _fake_stream():
        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=MagicMock(content="hi", reasoning_content=None))]
        chunk.usage = None
        yield chunk

    captured: dict = {}

    async def _capture_min_required(target, messages, max_tokens):
        captured["max_tokens"] = max_tokens
        return {"num_ctx": 8192}

    with patch("core.config.model_resolver.get_chat_target", return_value=_ollama_target()), \
         patch("tools.llm_gateway._supports_native_thinking", return_value=True), \
         patch.object(
             LLMGateway, "_resolve_local_num_ctx_kwarg",
             new=AsyncMock(side_effect=_capture_min_required),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_fake_stream())):
        async for _ in LLMGateway.astream_byom_thinking(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1000,
            enable_thinking=True,
            thinking_budget_tokens=4096,
        ):
            pass

    assert captured["max_tokens"] == 1000 + 4096
