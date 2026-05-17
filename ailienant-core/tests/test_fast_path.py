# ailienant-core/tests/test_fast_path.py
"""Phase 4.3 — Sequential bypass unit tests."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(content: str) -> MagicMock:
    """Construct a minimal litellm ModelResponse mock.

    Matches: response.choices[0].message.content → str
    This is the exact attribute chain used in fast_path.py and analyst.py.
    """
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# execute_sequential_bypass tests
# ---------------------------------------------------------------------------


async def test_sequential_bypass_returns_graph_shape() -> None:
    """Return dict must have 'messages' (2-entry list) and 'shared_understanding_reached=True'."""
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new_callable=AsyncMock,
        return_value=_make_llm_response("Hello world"),
    ):
        from brain.fast_path import execute_sequential_bypass
        result = await execute_sequential_bypass("what is X?", "/workspace")

    assert isinstance(result["messages"], list) and len(result["messages"]) == 2
    assert result["shared_understanding_reached"] is True
    # WebSocket contract: broadcast_token expects str — assert no None or empty content
    for msg in result["messages"]:
        assert isinstance(msg["content"], str) and msg["content"]


async def test_sequential_bypass_injects_soul_prompt() -> None:
    """SoulManager must be called; its output must become the LLM system message body."""
    with (
        patch("brain.personality.soul_manager") as mock_soul,
        patch(
            "tools.llm_gateway.LLMGateway.ainvoke",
            new_callable=AsyncMock,
            return_value=_make_llm_response("ok"),
        ) as mock_ainvoke,
    ):
        mock_soul.get_prompt.return_value = "TEST_PERSONA_PROMPT"
        from brain.fast_path import execute_sequential_bypass
        await execute_sequential_bypass("explain X", "/workspace")

    call_kwargs = mock_ainvoke.call_args.kwargs
    messages: list = call_kwargs["messages"]
    system_messages = [m for m in messages if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == "TEST_PERSONA_PROMPT"


async def test_sequential_bypass_fallback_on_llm_failure() -> None:
    """When LLMGateway.ainvoke raises, return echo stub — never propagate exception."""
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM offline"),
    ):
        from brain.fast_path import execute_sequential_bypass
        result = await execute_sequential_bypass("my prompt here", "/ws", task_id="t1")

    assert result["shared_understanding_reached"] is True
    assert "SEQUENTIAL STUB" in result["messages"][-1]["content"]
    assert isinstance(result["messages"][-1]["content"], str)


# ---------------------------------------------------------------------------
# process_user_intent routing tests
# ---------------------------------------------------------------------------


async def test_process_user_intent_routes_sequential() -> None:
    """process_user_intent with mode='sequential' must delegate to execute_sequential_bypass."""
    stub_result = {
        "messages": [{"role": "assistant", "content": "done"}],
        "shared_understanding_reached": True,
    }
    with patch(
        "brain.fast_path.execute_sequential_bypass",
        new_callable=AsyncMock,
        return_value=stub_result,
    ) as mock_bypass:
        from brain.engine import process_user_intent
        result = await process_user_intent("prompt", "/ws", execution_mode="sequential")

    mock_bypass.assert_awaited_once()
    assert result["shared_understanding_reached"] is True


async def test_process_user_intent_raises_for_swarm_modes() -> None:
    """MICRO_SWARM and FULL_SWARM must raise NotImplementedError (not yet wired)."""
    from brain.engine import process_user_intent
    for mode in ("MICRO_SWARM", "FULL_SWARM"):
        with pytest.raises(NotImplementedError, match="pending implementation"):
            await process_user_intent("prompt", "/ws", execution_mode=mode)
