"""CoderAgent run_command honesty — execute-tier steps must not lie as completed.

The coder has no live execute edge: it generates patches, it never spawns a
shell. A planned ``run_command`` step therefore cannot run. Reporting it as
``completed`` would deceive the operator into believing a command executed.
These tests pin the honest contract: ``run_command`` is surfaced as a failed,
deferred step (chip flips, review notes explain it), while ``read_file`` — which
genuinely has nothing to apply — still completes silently.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from agents.coder import run_coder_node
from brain.state import MissionSpecification, WBSStep


def _make_step(action: str, n: int = 1) -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role="core_dev",  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        target_file="main.py",
        description="Stub step.",
        status="pending",  # type: ignore[arg-type]
    )


def _make_state(step: WBSStep) -> Dict[str, Any]:
    mission = MissionSpecification(
        outcome="Test outcome.",
        scope=["main.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[step],
        checks=["Pytest exits 0."],
    )
    return {
        "task_id": "coder-defer-test",
        "mission_spec": mission,
        "current_step_id": step.step_number,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }


@pytest.fixture(autouse=True)
def _mock_emit() -> Any:
    """The skip branches return before any LLM/VFS/RAG I/O; only the WS notify
    is reached, so isolating emit_graph_mutation is sufficient."""
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.mark.anyio
async def test_run_command_step_marked_failed_not_completed() -> None:
    step = _make_step("run_command")
    await run_coder_node(_make_state(step))
    assert step.status == "failed"


@pytest.mark.anyio
async def test_run_command_step_emits_deferral_error() -> None:
    step = _make_step("run_command")
    result = await run_coder_node(_make_state(step))
    errors: List[str] = result.get("errors", [])
    assert errors, "expected a deferral note in errors"
    joined = " ".join(errors)
    assert "NOT" in joined and "executed" in joined
    assert "out-of-scope" in joined


@pytest.mark.anyio
async def test_run_command_step_emits_security_flag() -> None:
    step = _make_step("run_command")
    result = await run_coder_node(_make_state(step))
    flags: List[str] = result.get("security_flags", [])
    assert any(f.startswith("EXECUTE_TIER_DEFERRED:") for f in flags)


@pytest.mark.anyio
async def test_run_command_notifies_failed_status() -> None:
    step = _make_step("run_command")
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ) as emit:
        await run_coder_node(_make_state(step))
        # The notify is fire-and-forget (create_task); yield once so the
        # scheduled coroutine runs before we assert on it.
        await asyncio.sleep(0)
    emit.assert_awaited_once()
    assert emit.await_args is not None
    assert emit.await_args.kwargs.get("new_status") == "failed"


@pytest.mark.anyio
async def test_read_file_step_still_completes_silently() -> None:
    step = _make_step("read_file")
    result = await run_coder_node(_make_state(step))
    assert step.status == "completed"
    assert not result.get("errors")
    assert not any(
        f.startswith("EXECUTE_TIER_DEFERRED:")
        for f in result.get("security_flags", [])
    )
