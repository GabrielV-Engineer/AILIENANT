"""Multi-step WBS execution: the RELAY loop-back that runs every step in one turn.

Regression coverage for the orchestration gap where the production graph executed
only the first WBS step then terminated. Covers the three moving parts:
  - ``route_after_validation`` advances to the next pending step, routes to
    ``run_checks`` (which itself always advances to END) once all steps are
    terminal, retries a failed step, and stall-guards a non-advancing loop
    straight to END (no checks to run against an incomplete mission).
  - ``run_coder_node`` writes step status as a DURABLE, immutable ``mission_spec``
    delta (never in-place) so the checkpointed state advances reliably.
  - sequential same-file edits compose across steps while ``base_hash`` stays
    anchored to the true committed file.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

from brain.state import MissionSpecification, WBSStep
from brain.guardrails import route_after_validation

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _step(n: int, status: str = "pending", action: str = "edit_file",
          target_file: str = "f.py", depends_on: "List[int] | None" = None) -> WBSStep:
    return WBSStep(
        step_number=n, target_role="core_dev",
        action=action,  # type: ignore[arg-type]
        target_file=target_file, description=f"step {n}",
        status=status,  # type: ignore[arg-type]
        depends_on=depends_on,
    )


def _mission(tasks: List[WBSStep]) -> MissionSpecification:
    return MissionSpecification(
        outcome="o", scope=["f.py"], constraints=["c"],
        decisions=["d"], tasks=tasks, checks=["ok"],
    )


# ── route_after_validation ───────────────────────────────────────────────────


def test_advances_to_next_pending_step() -> None:
    # step 1 just completed; step 2 still pending → loop back to dispatch it.
    mission = _mission([_step(1, "completed"), _step(2, "pending")])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 1, "guardrail_failed": False}
    )
    assert target == "step_dispatch"


def test_routes_to_run_checks_when_all_steps_terminal_on_deep_effort() -> None:
    """Once every step is terminal, 'deep' effort routes to run_checks
    (brain/checks_gate.py) instead of ending directly — executing the plan's
    own acceptance criteria is that level's distinguishing feature."""
    mission = _mission([_step(1, "completed"), _step(2, "failed")])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 2, "guardrail_failed": False,
         "effort_level": "deep"}
    )
    assert target == "run_checks"


@pytest.mark.parametrize("effort_level", ["light", "balanced", None])
def test_light_and_balanced_effort_end_directly_skipping_checks(effort_level) -> None:  # type: ignore[no-untyped-def]
    """Only 'deep' pays for checks execution — light/balanced (and the
    default when effort_level was never set) end the turn here instead."""
    mission = _mission([_step(1, "completed"), _step(2, "failed")])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 2, "guardrail_failed": False,
         "effort_level": effort_level}
    )
    assert target == END


def test_guardrail_failure_retries_same_step() -> None:
    mission = _mission([_step(1, "completed"), _step(2, "pending")])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 2, "guardrail_failed": True,
         "retry_count": 1}
    )
    assert target == "coder_agent"


def test_stall_guard_ends_when_just_ran_step_still_pending() -> None:
    # The step we just executed is somehow still pending (durable-write failure /
    # early-return): must END, never re-loop the same step forever.
    mission = _mission([_step(1, "pending"), _step(2, "pending")])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 1, "guardrail_failed": False}
    )
    assert target == END


def test_no_mission_routes_to_run_checks_which_no_ops_gracefully() -> None:
    """No mission_spec at all still routes to run_checks on 'deep' effort (not
    a stall — there was simply nothing dispatchable), which itself no-ops
    safely on a missing mission and advances to END via its own edge."""
    assert route_after_validation(
        {"guardrail_failed": False, "effort_level": "deep"}
    ) == "run_checks"


def test_dependent_step_skipped_when_prerequisite_rejected() -> None:
    """DEBT-197: a step's depends_on names a REJECTED prerequisite — it must
    not be treated as dispatchable, so on 'deep' effort the loop routes to
    run_checks instead of sending it to a coder against a prerequisite that
    never landed."""
    mission = _mission([
        _step(1, "rejected"),
        _step(2, "pending", depends_on=[1]),
    ])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 1, "guardrail_failed": False,
         "effort_level": "deep"}
    )
    assert target == "run_checks"


def test_dependent_step_dispatches_once_prerequisite_completes() -> None:
    """Same shape, but the prerequisite genuinely completed — step 2 becomes
    dispatchable and the loop advances normally."""
    mission = _mission([
        _step(1, "completed"),
        _step(2, "pending", depends_on=[1]),
    ])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 1, "guardrail_failed": False}
    )
    assert target == "step_dispatch"


def test_unrelated_pending_step_still_dispatches_despite_blocked_sibling() -> None:
    """A step with an unmet dependency must not block an unrelated, independent
    pending step from dispatching."""
    mission = _mission([
        _step(1, "rejected"),
        _step(2, "pending", depends_on=[1]),
        _step(3, "pending"),
    ])
    target = route_after_validation(
        {"mission_spec": mission, "current_step_id": 1, "guardrail_failed": False}
    )
    assert target == "step_dispatch"


# ── run_coder_node: durable status delta (read_file path) ────────────────────


async def test_read_file_step_marks_completed_durably_not_in_place() -> None:
    from agents.coder import run_coder_node

    original = _mission([_step(1, "pending", action="read_file"), _step(2, "pending")])
    state: Dict[str, Any] = {
        "task_id": "t", "project_id": "", "workspace_root": "",
        "mission_spec": original, "current_step_id": 1,
    }
    with patch("api.websocket_manager.vfs_manager.emit_graph_mutation",
               new=AsyncMock(return_value=None)):
        result = await run_coder_node(state, {"configurable": {}})

    # Returned as a state delta (durable), not mutated in place.
    assert "mission_spec" in result
    returned: MissionSpecification = result["mission_spec"]
    assert returned.tasks[0].status == "completed"
    assert returned is not original                       # immutable copy
    assert original.tasks[0].status == "pending"          # caller's object untouched


# ── same-file edit composition ───────────────────────────────────────────────


async def test_sequential_same_file_edits_compose_with_true_base_hash() -> None:
    """A second step editing the same file anchors its SEARCH to the first step's
    in-run result, while base_hash stays the hash of the TRUE committed original."""
    from agents import coder as coder_mod
    from agents.coder import run_coder_node, content_hash

    true_original = "def fib(n):\n    return n\n"
    step1_result = "def fib(n):\n    return n  # refactored\n"

    mission = _mission([_step(1, "pending", action="edit_file", target_file="fib.py")])
    # Simulate step 1 already applied in this run (its content is in pending_contents).
    state: Dict[str, Any] = {
        "task_id": "t", "project_id": "", "workspace_root": "",
        "mission_spec": mission, "current_step_id": 1,
        "pending_contents": {"fib.py": step1_result},
    }

    # The committed VFS still holds the TRUE original (nothing applied to disk yet).
    def _fake_reader(project_id: str, workspace_root: str, session_id: str):
        return lambda p: true_original if p == "fib.py" else None

    # Model appends one line, anchoring on the ALREADY-refactored (step-1) content.
    edit_blob = (
        "### EDIT fib.py\n"
        "<<<<<<< SEARCH\n"
        "    return n  # refactored\n"
        "=======\n"
        "    return n  # refactored + gui\n"
        ">>>>>>> REPLACE\n"
    )
    with patch.object(coder_mod, "_make_vfs_reader", _fake_reader), \
         patch("tools.llm_gateway.LLMGateway.acomplete_with_thinking",
               new=AsyncMock(return_value=edit_blob)), \
         patch("core.response_cache.response_cache.probe", return_value=None), \
         patch("core.response_cache.response_cache.store", new=lambda *a, **k: None), \
         patch("api.websocket_manager.vfs_manager.emit_graph_mutation",
               new=AsyncMock(return_value=None)), \
         patch("agents.coder._fetch_rag_snippets", new=AsyncMock(return_value=[])), \
         patch("brain.coder_companion.schedule_coder_companion", new=lambda *a, **k: None):
        result = await run_coder_node(state, {"configurable": {}})

    contents = result.get("pending_contents", {})
    base_hash = result.get("pending_base_hash", {})
    assert "fib.py" in contents
    # The edit anchored on step-1 content → cumulative final includes the gui line.
    assert "refactored + gui" in contents["fib.py"]
    # base_hash is the TRUE committed original, so the write-pipeline stale guard
    # (which checks disk == base_hash) still passes.
    assert base_hash["fib.py"] == content_hash(true_original)
