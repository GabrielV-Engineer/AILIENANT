# ailienant-core/tests/test_llm_gateway_generation_telemetry.py
"""ainvoke emits output-side generation telemetry (13.1.3, B5).

Every prior telemetry record (`core/telemetry_log.py::log_context_utilization`)
measured only the INPUT side of a call — none said anything about what the
model actually generated. A live planner failure whose root cause was a
truncated, unparseable draft took a full log-forensics pass to diagnose
because `completion_tokens` and `finish_reason` were never recorded anywhere.
This pins that `ainvoke` now records both, plus the `num_ctx` the call was
actually served under, on every completion.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio


def _mock_response(prompt_tokens: int, completion_tokens: int, finish_reason: str) -> MagicMock:
    resp = MagicMock()
    resp.usage = MagicMock(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    resp.choices = [MagicMock(finish_reason=finish_reason)]
    return resp


async def test_ainvoke_records_generation_telemetry_on_a_clean_completion() -> None:
    from tools.llm_gateway import LLMGateway

    with patch(
        "litellm.acompletion",
        new=AsyncMock(return_value=_mock_response(1200, 400, "stop")),
    ), patch("core.telemetry_log.log_generation_utilization") as mock_log:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            session_id="sess-1",
        )

    mock_log.assert_called_once()
    kwargs = mock_log.call_args.kwargs
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["prompt_tokens"] == 1200
    assert kwargs["completion_tokens"] == 400
    assert kwargs["finish_reason"] == "stop"


async def test_ainvoke_records_a_truncated_completion_honestly() -> None:
    """The exact live-incident shape: finish_reason=length must be recorded,
    not just logged as a warning and discarded."""
    from tools.llm_gateway import LLMGateway

    with patch(
        "litellm.acompletion",
        new=AsyncMock(return_value=_mock_response(3000, 1096, "length")),
    ), patch("core.telemetry_log.log_generation_utilization") as mock_log:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            max_tokens=1096,
        )

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["finish_reason"] == "length"


async def test_ainvoke_records_the_resolved_num_ctx_for_a_local_target() -> None:
    from core.config.model_resolver import RuntimeCapabilities
    from tools.llm_gateway import LLMGateway

    target = MagicMock()
    target.model = "ollama_chat/gemma4:e4b"
    target.provider = "ollama"
    target.api_base = "http://localhost:11434"
    target.api_key = None
    target.is_local = True

    with patch("core.config.model_resolver.get_chat_target", return_value=target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response(1000, 300, "stop"))), \
         patch("core.telemetry_log.log_generation_utilization") as mock_log:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["num_ctx"] is not None
    assert mock_log.call_args.kwargs["num_ctx"] >= 4096


async def test_ainvoke_records_num_ctx_as_none_for_a_cloud_target() -> None:
    target = MagicMock()
    target.model = "gpt-4o"
    target.provider = "openai"
    target.api_base = None
    target.api_key = "sk-x"
    target.is_local = False

    from tools.llm_gateway import LLMGateway

    with patch("core.config.model_resolver.get_chat_target", return_value=target), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response(1000, 300, "stop"))), \
         patch("core.telemetry_log.log_generation_utilization") as mock_log:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["num_ctx"] is None


async def test_ainvoke_telemetry_fault_never_breaks_the_call() -> None:
    """A telemetry-sink fault must degrade silently — the caller's completion
    is the mission-critical path, telemetry is not."""
    from tools.llm_gateway import LLMGateway

    with patch(
        "litellm.acompletion",
        new=AsyncMock(return_value=_mock_response(100, 50, "stop")),
    ), patch(
        "core.telemetry_log.log_generation_utilization", side_effect=RuntimeError("sink down"),
    ):
        response = await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}], model="gpt-4o",
        )
    assert response is not None
