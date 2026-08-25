# ailienant-core/tests/test_mode_rbac_enforcement.py
"""Mode → RBAC enforcement at the write gate (ADR-728).

The frontend's three-way mode selector (automatic | ask_before_edits |
plan_mode) maps to a SessionPermissionMode that governs the incremental
per-step apply gate (brain/apply_gate.py, 13.0.9). The gate composes the
session mode with the WRITE tier and the coder's identity floor via the
existing ``evaluate_action`` matrix:

  * Plan  → DENY  : the step's change is discarded, no interrupt is ever
                    raised, and the write pipeline is never touched.
  * Ask   → HITL  : a native interrupt() approval card runs; apply only on
                    approval.
  * Auto  → ALLOW : the step's change auto-applies, no interrupt at all.

Sections 1-3 used to drive this end-to-end via a faked ``alienant_app.astream``
(that seam no longer runs any apply logic — it moved into the graph), so they
now call ``run_apply_prepare_node``/``run_apply_commit_node`` directly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node
from brain.state import MissionSpecification, WBSStep
from core.permissions import (
    PermissionDecision,
    PermissionMode,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    session_mode_from_frontend,
)
from core.task_service import TaskPayload


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


def _state(session_mode: str) -> Dict[str, Any]:
    """``session_permission_mode`` is uppercase here because the graph state
    channel stores it that way; the gate lowercases before building the enum."""
    return {
        "task_id": "s1",
        "project_id": "p1",
        "workspace_root": "/ws",
        "current_step_id": 1,
        "mission_spec": _mission(),
        "session_permission_mode": session_mode,
        "pending_step_files": {"1": ["calc.py"]},
        "pending_step_command": {},
        "pending_contents": {"calc.py": "def f():\n    return 2\n"},
        "pending_base_hash": {"calc.py": "deadbeef"},
        "auto_accept_low_risk": False,
        "applied_files_log": [],
        "applied_step_ids": [],
        "apply_attempts": {},
    }


def _payload(execution_mode: Optional[str]) -> TaskPayload:
    return TaskPayload(
        task_prompt="bump the increment",
        dirty_buffers=[],
        project_id=None,
        workspace_root="/ws",
        execution_mode=execution_mode,
    )


# ── 1. Plan → DENY ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_plan_mode_denies_write_without_card() -> None:
    prepared = await run_apply_prepare_node(_state("PLAN_ONLY"))
    assert prepared["pending_apply"]["decision"] == "deny"

    state = _state("PLAN_ONLY")
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock()
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_not_called()  # no HITL card in Plan mode
    apply_mock.assert_not_awaited()     # nothing applied
    assert committed["mission_spec"].tasks[0].status == "failed"
    assert "read-only" in committed["errors"][0]


# ── 2. Ask → HITL ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ask_mode_routes_through_hitl_and_applies_on_approval() -> None:
    prepared = await run_apply_prepare_node(_state("CAUTIOUS"))
    assert prepared["pending_apply"]["decision"] == "hitl"

    state = _state("CAUTIOUS")
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    with patch(
        "brain.apply_gate.request_graph_approval",
        return_value={"approved": True, "comment": None, "modified_content": None},
    ) as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_called_once()
    apply_mock.assert_awaited_once()
    assert committed["mission_spec"].tasks[0].status == "completed"


@pytest.mark.anyio
async def test_ask_mode_rejection_applies_nothing() -> None:
    state = _state("CAUTIOUS")
    state["pending_apply"] = {
        "step_number": 1, "kind": "FILE_WRITE", "decision": "hitl",
        "files": [{"file_path": "calc.py", "unified_diff": "@@ -1 +1 @@\n", "base_hash": "deadbeef"}],
        "command": None, "risk_labels": [], "auto_accept": False, "attempt": 0,
    }
    apply_mock = AsyncMock()
    with patch(
        "brain.apply_gate.request_graph_approval",
        return_value={"approved": False, "comment": None, "modified_content": None},
    ) as mock_interrupt, patch("core.write_pipeline.apply_patch_set", new=apply_mock):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_called_once()
    apply_mock.assert_not_awaited()
    assert committed["mission_spec"].tasks[0].status == "rejected"


# ── 3. Auto → ALLOW (no card, applies directly) ──────────────────────────────


@pytest.mark.anyio
async def test_auto_mode_auto_applies_without_card() -> None:
    prepared = await run_apply_prepare_node(_state("STANDARD"))
    assert prepared["pending_apply"]["decision"] == "allow"

    state = _state("STANDARD")
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_not_called()  # Auto skips the card entirely
    apply_mock.assert_awaited_once()
    # The actuation received the coder's original proposal.
    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == {"calc.py": "def f():\n    return 2\n"}
    assert committed["mission_spec"].tasks[0].status == "completed"


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
