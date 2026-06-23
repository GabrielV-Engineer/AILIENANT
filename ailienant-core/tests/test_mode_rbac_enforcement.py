# ailienant-core/tests/test_mode_rbac_enforcement.py
"""Mode → RBAC enforcement at the write gate (ADR-728).

The frontend's three-way mode selector (automatic | ask_before_edits |
plan_mode) maps to a SessionPermissionMode that governs the live write gate in
``_run_coding_task``. The gate composes the session mode with the WRITE tier and
the coder's identity floor via the existing ``evaluate_action`` matrix:

  * Plan  → DENY  : the change set is discarded, the HITL card is never shown,
                    and the write pipeline is never touched.
  * Ask   → HITL  : the approval card runs; apply only on approval.
  * Auto  → ALLOW : the change set auto-applies, with an explicit "auto-applying"
                    notice emitted BEFORE the disk I/O (no silent mutation).

These patch the engine at the ``astream`` seam (yielding a crafted final state
that carries ``session_permission_mode``, exactly as a real graph run would) so
the gate's control flow is exercised in isolation from the agents.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep
from core.permissions import (
    PermissionDecision,
    PermissionMode,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    session_mode_from_frontend,
)
from core.task_service import TaskPayload, TaskService


def _mission() -> MissionSpecification:
    return MissionSpecification(
        outcome="Bump the increment.",
        scope=["calc.py"],
        constraints=["none"],
        decisions=["go"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="core_dev",
                action="edit_file",
                target_file="calc.py",
                description="bump",
            )
        ],
        checks=["ok"],
    )


def _final_state(session_mode: str) -> Dict[str, Any]:
    """A finished plan + one merged coder patch, tagged with a session mode.

    ``session_permission_mode`` is uppercase here because the graph state channel
    is ``Literal["DEFAULT","PLAN","AUTO"]``; the gate lowercases before building
    the enum.
    """
    return {
        "mission_spec": _mission(),
        "pending_patches": {"calc.py": "--- a/calc.py\n+++ b/calc.py\n"},
        "pending_contents": {"calc.py": "def f():\n    return 2\n"},
        "pending_base_hash": {"calc.py": "deadbeef"},
        "errors": [],
        "hitl_pending": False,
        "session_permission_mode": session_mode,
    }


def _fake_astream(final_state: Dict[str, Any]) -> Any:
    def _factory(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield final_state

        return _gen()

    return _factory


def _payload(execution_mode: Optional[str]) -> TaskPayload:
    return TaskPayload(
        task_prompt="bump the increment",
        dirty_buffers=[],
        project_id=None,
        workspace_root="/ws",
        execution_mode=execution_mode,
    )


def _gate_patches(
    *,
    session_mode: str,
    approval: Optional[Dict[str, Any]],
    apply_mock: AsyncMock,
    approval_mock: AsyncMock,
    token_mock: AsyncMock,
) -> List[Any]:
    return [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream(_final_state(session_mode))),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=token_mock),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
    ]


# ── 1. Plan → DENY ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_plan_mode_denies_write_without_card() -> None:
    apply_mock = AsyncMock()
    approval_mock = AsyncMock()
    token_mock = AsyncMock()
    ctxs = _gate_patches(
        session_mode="PLAN", approval=None,
        apply_mock=apply_mock, approval_mock=approval_mock, token_mock=token_mock,
    )
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s-plan", _payload("plan_mode"), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    approval_mock.assert_not_awaited()  # no HITL card in Plan mode
    apply_mock.assert_not_awaited()     # nothing applied
    # The read-only refusal was streamed.
    assert any("read-only" in str(c) for c in token_mock.await_args_list)


# ── 2. Ask → HITL ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ask_mode_routes_through_hitl_and_applies_on_approval() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    approval_mock = AsyncMock(return_value={"approved": True, "comment": None, "modified_content": None})
    ctxs = _gate_patches(
        session_mode="DEFAULT", approval=None,
        apply_mock=apply_mock, approval_mock=approval_mock, token_mock=AsyncMock(),
    )
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s-ask", _payload("ask_before_edits"), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    approval_mock.assert_awaited_once()
    apply_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_ask_mode_rejection_applies_nothing() -> None:
    apply_mock = AsyncMock()
    approval_mock = AsyncMock(return_value={"approved": False, "comment": None, "modified_content": None})
    ctxs = _gate_patches(
        session_mode="DEFAULT", approval=None,
        apply_mock=apply_mock, approval_mock=approval_mock, token_mock=AsyncMock(),
    )
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s-ask2", _payload("ask_before_edits"), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    approval_mock.assert_awaited_once()
    apply_mock.assert_not_awaited()


# ── 3. Auto → ALLOW (no card, announced) ─────────────────────────────────────


@pytest.mark.anyio
async def test_auto_mode_auto_applies_without_card_and_announces() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    approval_mock = AsyncMock()
    token_mock = AsyncMock()
    ctxs = _gate_patches(
        session_mode="AUTO", approval=None,
        apply_mock=apply_mock, approval_mock=approval_mock, token_mock=token_mock,
    )
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s-auto", _payload("automatic"), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    approval_mock.assert_not_awaited()  # Auto skips the card
    apply_mock.assert_awaited_once()
    # The actuation received the coder's original proposal (guards the decouple
    # fix against an empty-dataset apply in the no-card path).
    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == {"calc.py": "def f():\n    return 2\n"}
    # The "auto-applying" intent was streamed before the write completed.
    assert any("Auto-applying" in str(c) for c in token_mock.await_args_list)


# ── 4. Mapping helper ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "frontend, expected",
    [
        ("automatic", SessionPermissionMode.STANDARD),
        ("ask_before_edits", SessionPermissionMode.CAUTIOUS),
        ("plan_mode", SessionPermissionMode.PLAN_ONLY),
        ("PLAN_MODE", SessionPermissionMode.PLAN_ONLY),  # case-insensitive
        ("nonsense", None),
        ("", None),
        (None, None),
    ],
)
def test_session_mode_from_frontend(frontend: Optional[str], expected: Optional[SessionPermissionMode]) -> None:
    assert session_mode_from_frontend(frontend) is expected


# ── 5. submit_task wiring (plan_mode forces planner_mode_active) ──────────────


@pytest.mark.anyio
async def test_submit_plan_mode_forces_planner_flag() -> None:
    import asyncio

    import main

    captured: Dict[str, Any] = {}

    async def _capture(*, session_id: str, payload: TaskPayload, execution_mode: str) -> Dict[str, Any]:
        captured["planner_mode_active"] = payload.planner_mode_active
        return {"status": "success"}

    body = TaskPayload(task_prompt="x", dirty_buffers=[], workspace_root="/ws", execution_mode="plan_mode")
    with patch.object(main.task_service, "process_task", side_effect=_capture), \
         patch("main._get_hw_profile", new=AsyncMock(return_value=type("H", (), {"suggested_mode": "SEQUENTIAL"})())), \
         patch("main.get_execution_mode_pref", return_value="SEQUENTIAL"):
        await main.submit_task(body, x_task_id="sess-plan-mode")
        for _ in range(50):
            if "planner_mode_active" in captured:
                break
            await asyncio.sleep(0.01)

    assert captured.get("planner_mode_active") is True


@pytest.mark.anyio
async def test_submit_auto_mode_leaves_planner_flag_false() -> None:
    import asyncio

    import main

    captured: Dict[str, Any] = {}

    async def _capture(*, session_id: str, payload: TaskPayload, execution_mode: str) -> Dict[str, Any]:
        captured["planner_mode_active"] = payload.planner_mode_active
        return {"status": "success"}

    body = TaskPayload(task_prompt="x", dirty_buffers=[], workspace_root="/ws", execution_mode="automatic")
    with patch.object(main.task_service, "process_task", side_effect=_capture), \
         patch("main._get_hw_profile", new=AsyncMock(return_value=type("H", (), {"suggested_mode": "SEQUENTIAL"})())), \
         patch("main.get_execution_mode_pref", return_value="SEQUENTIAL"):
        await main.submit_task(body, x_task_id="sess-auto-mode")
        for _ in range(50):
            if "planner_mode_active" in captured:
                break
            await asyncio.sleep(0.01)

    assert captured.get("planner_mode_active") is False


# ── 6. Matrix focus — the 3-axis verdict the wiring relies on ────────────────


def test_evaluate_action_matrix_contract() -> None:
    coder = PermissionMode.EDIT_EXECUTE_RBW
    assert evaluate_action(SessionPermissionMode.PLAN, ToolPrivilegeTier.WRITE, coder) is PermissionDecision.DENY
    assert evaluate_action(SessionPermissionMode.DEFAULT, ToolPrivilegeTier.WRITE, coder) is PermissionDecision.HITL
    assert evaluate_action(SessionPermissionMode.AUTO, ToolPrivilegeTier.WRITE, coder) is PermissionDecision.ALLOW
    # READ_ONLY is always allowed regardless of session mode.
    assert evaluate_action(SessionPermissionMode.PLAN, ToolPrivilegeTier.READ_ONLY, coder) is PermissionDecision.ALLOW
