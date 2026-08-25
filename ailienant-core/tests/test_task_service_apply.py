# ailienant-core/tests/test_task_service_apply.py
"""The incremental per-step apply gate (brain/apply_gate.py, 13.0.9).

Replaces the old post-graph replay this file used to test (a single pass over
the WHOLE WBS's frozen ``pending_patches`` dict, driven by mocking
``alienant_app.astream`` to yield one crafted final state — the actual apply
logic lived entirely in ``core/task_service.py`` at the time). That logic now
lives in two graph nodes, ``run_apply_prepare_node`` (PREPARE, no interrupt)
and ``run_apply_commit_node`` (GATE, interrupt-first), gating ONE step at a
time — so these tests call the nodes directly with hand-built state, exactly
as ``brain/finops.py``'s own interrupt-bearing node is tested in
``test_finops.py``.

Approved ⇒ the write pipeline is invoked with the step's own content + base
hash. Rejected ⇒ nothing is applied. Requesting changes ⇒ re-dispatched with
feedback, bounded by ``APPLY_REJECT_MAX_ATTEMPTS``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional
from unittest.mock import AsyncMock, patch

import pytest

from agents.coder import content_hash
from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node
from brain.retry_policy import APPLY_REJECT_MAX_ATTEMPTS, CORRECTION_MAX_ATTEMPTS
from brain.state import MissionSpecification, WBSStep

pytestmark = pytest.mark.anyio


def _mission(
    action: Literal["read_file", "write_file", "edit_file", "run_command"] = "edit_file",
    target_file: str = "calc.py",
) -> MissionSpecification:
    return MissionSpecification(
        outcome="Bump the increment.",
        scope=[target_file],
        constraints=["none"],
        decisions=["go"],
        tasks=[
            WBSStep(
                step_number=1, target_role="core_dev", action=action,
                target_file=target_file, description="bump",
            )
        ],
        checks=["ok"],
    )


def _base_state(
    *,
    mission: Optional[MissionSpecification] = None,
    session_permission_mode: str = "STANDARD",
    pending_step_files: Optional[Dict[str, List[str]]] = None,
    pending_step_command: Optional[Dict[str, str]] = None,
    pending_contents: Optional[Dict[str, str]] = None,
    pending_base_hash: Optional[Dict[str, str]] = None,
    auto_accept_low_risk: bool = False,
    applied_files_log: Optional[List[Dict[str, Any]]] = None,
    apply_attempts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "task_id": "s1",
        "project_id": "p1",
        "workspace_root": "/ws",
        "current_step_id": 1,
        "mission_spec": mission or _mission(),
        "session_permission_mode": session_permission_mode,
        "pending_step_files": pending_step_files or {},
        "pending_step_command": pending_step_command or {},
        "pending_contents": pending_contents or {},
        "pending_base_hash": pending_base_hash or {},
        "auto_accept_low_risk": auto_accept_low_risk,
        "applied_files_log": applied_files_log or [],
        "applied_step_ids": [],
        "apply_attempts": apply_attempts or {},
    }


@pytest.fixture(autouse=True)
def _no_blast_radius() -> Any:
    """The blast-radius mapper hits real project data — stub it empty so
    every prepare test is isolated from it unless a test explicitly overrides
    it to exercise the escalation path."""
    with patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=[])):
        yield


# ──────────────────────────────────────────────────────────────────────────
# run_apply_prepare_node — FILE_WRITE
# ──────────────────────────────────────────────────────────────────────────


async def test_prepare_file_write_allow_mode_commits_no_interrupt_pending() -> None:
    state = _base_state(
        session_permission_mode="STANDARD",
        pending_step_files={"1": ["calc.py"]},
        pending_contents={"calc.py": "def f():\n    return 2\n"},
        pending_base_hash={"calc.py": "deadbeef"},
    )
    result = await run_apply_prepare_node(state)
    env = result["pending_apply"]
    assert env["kind"] == "FILE_WRITE"
    assert env["decision"] == "allow"
    assert env["files"][0]["file_path"] == "calc.py"
    assert env["files"][0]["base_hash"] == "deadbeef"
    # ALLOW never needs a card — status is untouched (still whatever the coder set).
    assert result["mission_spec"].tasks[0].status == "pending"


async def test_prepare_file_write_hitl_mode_marks_awaiting_approval() -> None:
    state = _base_state(
        session_permission_mode="CAUTIOUS",
        pending_step_files={"1": ["calc.py"]},
        pending_contents={"calc.py": "def f():\n    return 2\n"},
    )
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["decision"] == "hitl"
    assert result["mission_spec"].tasks[0].status == "awaiting_approval"


async def test_prepare_file_write_carries_a_real_unified_diff() -> None:
    state = _base_state(
        pending_step_files={"1": ["calc.py"]},
        pending_contents={"calc.py": "def f():\n    return 2\n"},
        pending_base_hash={"calc.py": "deadbeef"},
    )
    result = await run_apply_prepare_node(state)
    diff = result["pending_apply"]["files"][0]["unified_diff"]
    added = "".join(
        ln[1:] for ln in diff.splitlines(keepends=True)
        if ln.startswith("+") and not ln.startswith("+++")
    )
    assert added == "def f():\n    return 2\n"


async def test_prepare_only_internal_path_fails_the_step_outright() -> None:
    with patch("brain.apply_gate.is_ailienant_internal_path", side_effect=lambda p: p.endswith(".log")):
        state = _base_state(
            pending_step_files={"1": [".ailienant_telemetry.log"]},
            pending_contents={".ailienant_telemetry.log": "x"},
        )
        result = await run_apply_prepare_node(state)
    assert "pending_apply" not in result
    assert result["mission_spec"].tasks[0].status == "failed"
    assert any("runtime files" in e for e in result["errors"])


async def test_prepare_auto_accept_low_risk_flips_to_auto_accept() -> None:
    state = _base_state(
        session_permission_mode="CAUTIOUS",
        pending_step_files={"1": ["calc.py"]},
        pending_contents={"calc.py": "def f():\n    return 2\n"},
        auto_accept_low_risk=True,
    )
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["auto_accept"] is True
    # auto_accept skips the card, so the checklist never shows awaiting_approval.
    assert result["mission_spec"].tasks[0].status == "pending"


async def test_prepare_auto_accept_still_requires_a_card_when_content_is_risky() -> None:
    """A secret-access pattern in the added lines forces the manual round-trip
    even with auto_accept_low_risk on — the conservative floor."""
    state = _base_state(
        session_permission_mode="CAUTIOUS",
        pending_step_files={"1": ["calc.py"]},
        pending_contents={"calc.py": "API_KEY = fetch_secret()\n"},
        auto_accept_low_risk=True,
    )
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["auto_accept"] is False
    assert result["pending_apply"]["risk_labels"]
    assert result["mission_spec"].tasks[0].status == "awaiting_approval"


async def test_prepare_blast_radius_exceeded_escalates_allow_to_hitl() -> None:
    from shared.config import BLAST_RADIUS_THRESHOLD_FILES

    affected = [f"dep_{i}.py" for i in range(BLAST_RADIUS_THRESHOLD_FILES + 5)]
    with patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=affected)):
        state = _base_state(
            session_permission_mode="STANDARD",  # would otherwise be ALLOW, no card
            pending_step_files={"1": ["calc.py"]},
            pending_contents={"calc.py": "x = 1\n"},
        )
        result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["decision"] == "hitl"
    assert result["pending_apply"]["blast_radius_files"] == affected
    assert result["mission_spec"].tasks[0].status == "awaiting_approval"


async def test_prepare_pre_patch_veto_fails_the_step_before_any_interrupt() -> None:
    """ALLOW/auto-accept must run pre_patch BEFORE committing pending_apply —
    a vetoed write must never reach apply_commit at all."""
    with patch(
        "core.task_service.run_patch_hooks",
        new=AsyncMock(side_effect=[(False, ["ruff check . failed"])]),
    ):
        state = _base_state(
            session_permission_mode="STANDARD",
            pending_step_files={"1": ["calc.py"]},
            pending_contents={"calc.py": "x = 1\n"},
        )
        result = await run_apply_prepare_node(state)
    assert "pending_apply" not in result
    assert result["mission_spec"].tasks[0].status == "failed"
    assert any("pre_patch hook vetoed" in e for e in result["errors"])


async def test_prepare_orphaned_in_progress_with_nothing_staged_fails_honestly() -> None:
    """The second-audit-pass backstop: a step that reached the apply gate
    in_progress with neither a file nor a command must fail loudly, not sit
    forever unresolved (which would defeat route_after_validation's stall
    guard silently)."""
    state = _base_state(pending_step_files={}, pending_step_command={})
    result = await run_apply_prepare_node(state)
    assert result["mission_spec"].tasks[0].status == "failed"
    assert "nothing was generated" in result["errors"][0]


async def test_prepare_no_op_when_step_already_terminal() -> None:
    mission = _mission()
    mission.tasks[0].status = "completed"
    state = _base_state(mission=mission)
    result = await run_apply_prepare_node(state)
    assert result == {}


# ──────────────────────────────────────────────────────────────────────────
# run_apply_prepare_node — COMMAND_EXECUTE
# ──────────────────────────────────────────────────────────────────────────


async def test_prepare_command_deny_mode() -> None:
    state = _base_state(session_permission_mode="PLAN_ONLY", pending_step_command={"1": "pytest -q"})
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["decision"] == "deny"
    assert result["pending_apply"]["command"] == "pytest -q"


async def test_prepare_command_hitl_mode_marks_awaiting_approval() -> None:
    state = _base_state(session_permission_mode="CAUTIOUS", pending_step_command={"1": "pytest -q"})
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["decision"] == "hitl"
    assert result["pending_apply"]["kind"] == "COMMAND_EXECUTE"
    assert result["mission_spec"].tasks[0].status == "awaiting_approval"


async def test_prepare_command_dangerous_pattern_upgrades_to_risk_intercept() -> None:
    state = _base_state(session_permission_mode="STANDARD", pending_step_command={"1": "rm -rf /"})
    result = await run_apply_prepare_node(state)
    assert result["pending_apply"]["kind"] == "RISK_INTERCEPT"
    assert result["pending_apply"]["decision"] == "hitl"


# ──────────────────────────────────────────────────────────────────────────
# run_apply_commit_node — routing / idempotency
# ──────────────────────────────────────────────────────────────────────────


async def test_commit_no_pending_apply_is_a_no_op() -> None:
    state = _base_state()
    assert await run_apply_commit_node(state) == {}


async def test_commit_already_applied_step_is_idempotent() -> None:
    state = _base_state(applied_files_log=[{"status": "completed"}])
    state["applied_step_ids"] = [1]
    state["pending_apply"] = {"step_number": 1, "kind": "FILE_WRITE", "decision": "allow", "files": []}
    result = await run_apply_commit_node(state)
    assert result == {"pending_apply": None}


async def test_commit_deny_fails_without_any_interrupt() -> None:
    state = _base_state()
    state["pending_apply"] = {
        "step_number": 1, "kind": "FILE_WRITE", "decision": "deny",
        "files": [], "command": None, "risk_labels": [], "auto_accept": False, "attempt": 0,
    }
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt:
        result = await run_apply_commit_node(state)
    mock_interrupt.assert_not_called()
    assert result["mission_spec"].tasks[0].status == "failed"
    assert result["applied_step_ids"] == [1]


# ──────────────────────────────────────────────────────────────────────────
# run_apply_commit_node — FILE_WRITE, the interrupt itself
# ──────────────────────────────────────────────────────────────────────────


def _file_write_env(**overrides: Any) -> Dict[str, Any]:
    env = {
        "step_number": 1, "kind": "FILE_WRITE", "decision": "hitl",
        "files": [{"file_path": "calc.py", "unified_diff": "@@ -1 +1 @@\n-old\n+new\n", "base_hash": "deadbeef"}],
        "command": None, "risk_labels": [], "auto_accept": False, "attempt": 0,
    }
    env.update(overrides)
    return env


async def test_commit_hitl_file_write_interrupts_with_proposed_files() -> None:
    state = _base_state(pending_contents={"calc.py": "def f():\n    return 2\n"})
    state["pending_apply"] = _file_write_env()
    with patch(
        "brain.apply_gate.request_graph_approval",
        return_value={"approved": True, "comment": None, "modified_content": None},
    ) as mock_interrupt, patch(
        "core.write_pipeline.apply_patch_set",
        new=AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]}),
    ), patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        result = await run_apply_commit_node(state)

    mock_interrupt.assert_called_once()
    kwargs = mock_interrupt.call_args.kwargs
    assert kwargs["request_kind"] == "FILE_WRITE"
    proposed = kwargs["proposed_files"]
    assert proposed is not None and proposed[0].file_path == "calc.py"
    assert proposed[0].unified_diff == "@@ -1 +1 @@\n-old\n+new\n"
    assert proposed[0].base_hash == "deadbeef"
    assert result["mission_spec"].tasks[0].status == "completed"
    assert result["applied_step_ids"] == [1]


async def test_commit_allow_never_interrupts() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(decision="allow")
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, patch(
        "core.write_pipeline.apply_patch_set",
        new=AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]}),
    ), patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        result = await run_apply_commit_node(state)
    mock_interrupt.assert_not_called()
    assert result["mission_spec"].tasks[0].status == "completed"


async def test_commit_auto_accept_never_interrupts() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(auto_accept=True)
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, patch(
        "core.write_pipeline.apply_patch_set",
        new=AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]}),
    ), patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        result = await run_apply_commit_node(state)
    mock_interrupt.assert_not_called()
    assert result["mission_spec"].tasks[0].status == "completed"


async def test_commit_approved_writes_this_steps_content_and_reanchors_hash() -> None:
    state = _base_state(pending_contents={"calc.py": "def f():\n    return 2\n"})
    state["pending_apply"] = _file_write_env()
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        result = await run_apply_commit_node(state)

    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    _sid, contents, base_hashes = apply_mock.await_args.args[:3]
    assert contents == {"calc.py": "def f():\n    return 2\n"}
    assert base_hashes == {"calc.py": "deadbeef"}
    assert result["pending_base_hash"]["calc.py"] == content_hash("def f():\n    return 2\n")


async def test_commit_modified_content_from_the_cards_edit_mode_is_honored() -> None:
    state = _base_state(pending_contents={"calc.py": "def f():\n    return 2\n"})
    state["pending_apply"] = _file_write_env()
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": "EDITED\n"}), \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        await run_apply_commit_node(state)

    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == {"calc.py": "EDITED\n"}


async def test_commit_hitl_approved_runs_pre_patch_after_the_interrupt() -> None:
    """Prepare only ran pre_patch for ALLOW/auto-accept — a manually-approved
    HITL write must still be gated by it, just after the decision is known."""
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env()
    hooks_mock = AsyncMock(return_value=(False, ["ruff check . failed"]))
    apply_mock = AsyncMock()
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=hooks_mock):
        result = await run_apply_commit_node(state)

    apply_mock.assert_not_awaited()
    assert result["mission_spec"].tasks[0].status == "failed"
    assert any("pre_patch hook vetoed" in e for e in result["errors"])


async def test_commit_rejected_with_no_comment_is_terminal_and_never_applies() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env()
    apply_mock = AsyncMock()
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": False, "comment": None, "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock):
        result = await run_apply_commit_node(state)
    apply_mock.assert_not_awaited()
    assert result["mission_spec"].tasks[0].status == "rejected"
    assert result["applied_step_ids"] == [1]


async def test_commit_request_changes_re_dispatches_with_feedback() -> None:
    """The P3 regression, relocated: this used to be about NOT discarding
    already-approved files 1-3 when file 4 was declined with feedback — now
    structurally impossible, since each step is already on disk by the time
    the NEXT step's card appears. This step's own outcome is a bounded,
    honest re-dispatch instead."""
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env()
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": False, "comment": "use a different variable name", "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=AsyncMock()) as apply_mock:
        result = await run_apply_commit_node(state)

    apply_mock.assert_not_awaited()
    assert result["mission_spec"].tasks[0].status == "revision_requested"
    assert result["apply_feedback"] == {"step_number": 1, "comment": "use a different variable name", "attempt": 1}
    assert result["apply_attempts"] == {"1": 1}


async def test_commit_request_changes_degrades_to_rejected_past_the_attempt_ceiling() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(attempt=APPLY_REJECT_MAX_ATTEMPTS)
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": False, "comment": "try again", "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=AsyncMock()) as apply_mock:
        result = await run_apply_commit_node(state)
    apply_mock.assert_not_awaited()
    assert result["mission_spec"].tasks[0].status == "rejected"
    assert "attempt limit" in result["errors"][0]


async def test_commit_apply_stale_files_fails_with_the_re_run_message() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(decision="allow")
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=AsyncMock(return_value={"ok": False, "stale_files": ["calc.py"]})), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        result = await run_apply_commit_node(state)
    mock_interrupt.assert_not_called()
    assert result["mission_spec"].tasks[0].status == "failed"
    assert "changed since the proposal" in result["errors"][0]


async def test_commit_diff_activity_marker_fires_per_applied_file() -> None:
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(decision="allow")
    push_activity = AsyncMock()
    config = {"configurable": {"push_activity": push_activity}}
    with patch("brain.apply_gate.request_graph_approval"), \
         patch("core.write_pipeline.apply_patch_set", new=AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        await run_apply_commit_node(state, config)  # type: ignore[arg-type]

    push_activity.assert_awaited_once()
    assert push_activity.await_args is not None
    assert push_activity.await_args.args[0] == "diff"
    assert push_activity.await_args.kwargs["target"] == "calc.py"
    assert push_activity.await_args.kwargs["ref"] == "calc.py"


async def test_commit_post_patch_failure_is_advisory_write_still_landed() -> None:
    """decision=allow already ran pre_patch in prepare, so commit's own
    single run_patch_hooks call here is the post_patch one — its failure must
    only annotate the result, never undo the already-landed write."""
    state = _base_state(pending_contents={"calc.py": "x = 1\n"})
    state["pending_apply"] = _file_write_env(decision="allow")
    with patch("brain.apply_gate.request_graph_approval"), \
         patch("core.write_pipeline.apply_patch_set", new=AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(False, ["notify-fail"]))):
        result = await run_apply_commit_node(state)
    assert result["mission_spec"].tasks[0].status == "completed"
    assert "post_patch hook notes" in result["errors"][0]


# ──────────────────────────────────────────────────────────────────────────
# run_apply_commit_node — COMMAND_EXECUTE
# ──────────────────────────────────────────────────────────────────────────


def _command_env(**overrides: Any) -> Dict[str, Any]:
    env = {
        "step_number": 1, "kind": "COMMAND_EXECUTE", "decision": "hitl",
        "files": [], "command": "pytest -q", "risk_labels": [], "auto_accept": False, "attempt": 0,
    }
    env.update(overrides)
    return env


async def test_commit_command_success_marks_completed() -> None:
    state = _base_state()
    state["pending_apply"] = _command_env()
    ok_result = SimpleNamespace(exit_code=0, stdout="2 passed", stderr="")
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": None}), \
         patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=ok_result)):
        result = await run_apply_commit_node(state)
    assert result["mission_spec"].tasks[0].status == "completed"
    assert result["applied_files_log"][0]["command"] == "pytest -q"


async def test_commit_command_failure_triggers_healing_within_budget() -> None:
    state = _base_state(mission=_mission(action="run_command", target_file="pytest -q"))
    state["pending_apply"] = _command_env()
    fail_result = SimpleNamespace(exit_code=1, stdout="", stderr="1 failed")
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": None}), \
         patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=fail_result)):
        result = await run_apply_commit_node(state)
    assert result["healing_required"] is True
    assert result["failed_node"] == "apply_commit"
    assert result["correction_attempts"] == 1
    assert result["mission_spec"].tasks[0].status == "failed"


async def test_commit_command_failure_gives_up_past_correction_budget() -> None:
    state = _base_state(mission=_mission(action="run_command", target_file="pytest -q"))
    state["correction_attempts"] = CORRECTION_MAX_ATTEMPTS
    state["pending_apply"] = _command_env()
    fail_result = SimpleNamespace(exit_code=1, stdout="", stderr="1 failed")
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": True, "comment": None, "modified_content": None}), \
         patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=fail_result)):
        result = await run_apply_commit_node(state)
    assert "healing_required" not in result
    assert result["mission_spec"].tasks[0].status == "failed"
    assert "still failing after" in result["errors"][0]


async def test_commit_command_deny_never_calls_run_guarded_command() -> None:
    state = _base_state()
    state["pending_apply"] = _command_env(decision="deny")
    with patch("tools.execution_tools.run_guarded_command", new=AsyncMock()) as run_mock:
        result = await run_apply_commit_node(state)
    run_mock.assert_not_called()
    assert result["mission_spec"].tasks[0].status == "failed"
