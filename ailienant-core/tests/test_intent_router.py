# ailienant-core/tests/test_intent_router.py
"""Phase 4.3 stage-2 — process_user_intent dispatch tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio


async def test_routes_sequential() -> None:
    """SEQUENTIAL must delegate to execute_sequential_bypass."""
    stub = {
        "messages": [{"role": "assistant", "content": "done"}],
        "shared_understanding_reached": True,
    }
    with patch(
        "brain.fast_path.execute_sequential_bypass",
        new_callable=AsyncMock,
        return_value=stub,
    ) as mock_bypass:
        from brain.intent_router import process_user_intent

        result = await process_user_intent("prompt", "/ws", execution_mode="sequential")

    mock_bypass.assert_awaited_once()
    assert result["shared_understanding_reached"] is True


async def test_routes_micro_swarm() -> None:
    """MICRO_SWARM must invoke the compiled _MICRO_SWARM_APP with locked execution_mode."""
    stub = {"messages": [{"role": "assistant", "content": "ok"}]}
    with patch(
        "brain.swarms._MICRO_SWARM_APP.ainvoke",
        new_callable=AsyncMock,
        return_value=stub,
    ) as mock_app:
        from brain.intent_router import process_user_intent

        result = await process_user_intent("prompt", "/ws", execution_mode="MICRO_SWARM")

    mock_app.assert_awaited_once()
    initial = mock_app.call_args.args[0]
    assert initial["execution_mode"] == "MICRO_SWARM"
    assert initial["error_streak"] == 0
    assert initial["cloud_surgeon_invocations"] == 0
    assert initial["style_bypass_active"] is False
    assert result is stub


async def test_routes_full_swarm() -> None:
    """FULL_SWARM must compile a graph with checkpoint_manager and pass thread_id config."""
    fake_app = MagicMock()
    fake_app.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "brain.swarms.build_full_swarm", return_value=fake_app
    ) as mock_build:
        from brain.intent_router import process_user_intent

        await process_user_intent(
            "prompt", "/ws", task_id="t1", execution_mode="FULL_SWARM"
        )

    mock_build.assert_called_once()
    fake_app.ainvoke.assert_awaited_once()
    call_kwargs = fake_app.ainvoke.call_args.kwargs
    assert call_kwargs["config"] == {"configurable": {"thread_id": "t1"}}
    initial = fake_app.ainvoke.call_args.args[0]
    assert initial["execution_mode"] == "FULL_SWARM"


async def test_unknown_mode_raises() -> None:
    """Unrecognised execution_mode must raise NotImplementedError."""
    from brain.intent_router import process_user_intent

    with pytest.raises(NotImplementedError, match="not recognised"):
        await process_user_intent("prompt", "/ws", execution_mode="HYPER_SWARM")
