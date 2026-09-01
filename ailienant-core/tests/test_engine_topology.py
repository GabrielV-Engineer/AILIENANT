# ailienant-core/tests/test_engine_topology.py
#
# Regression guard for a PLAN_ONLY session's turn continuing past planner_agent
# into step_dispatch/CoderAgent execution instead of stopping at END. The bug:
# engine.py's planner_agent -> step_dispatch edge was a static, unconditional
# edge -- there was no gate reading session_permission_mode at all. CoderAgent's
# own RBAC check (agents/coder.py) denies each individual write/execute action
# under PLAN_ONLY, but that never stopped the graph from running and narrating
# every WBS step to completion, which is why a plan-mode turn looked like it was
# auto-executing an unapproved plan and its accept/reject buttons stayed
# disabled (isTurnActive never cleared) until that whole pass finished.

from typing import Any, Dict, cast

from langgraph.graph import END

from brain.engine import route_after_planner
from brain.state import AIlienantGraphState


def _state(session_permission_mode: Any) -> Dict[str, Any]:
    # A mission_spec is always present: route_after_planner's no-plan guard ends
    # the turn before the mode check, so a plan-less state exercises that guard
    # rather than the mode routing these tests are about.
    return {
        "task_id": "t1",
        "project_id": "p1",
        "session_permission_mode": session_permission_mode,
        "mission_spec": object(),
    }


def test_route_after_planner_stops_at_end_for_plan_only() -> None:
    assert route_after_planner(_state("PLAN_ONLY")) == END


def test_route_after_planner_stops_at_end_for_the_deprecated_plan_alias() -> None:
    # "PLAN" is the deprecated channel value that normalize_session_mode migrates
    # onto PLAN_ONLY -- a raw comparison against PLAN_ONLY would silently miss it.
    assert route_after_planner(_state("PLAN")) == END


def test_route_after_planner_continues_to_step_dispatch_for_standard_mode() -> None:
    assert route_after_planner(_state("DEFAULT")) == "step_dispatch"


def test_route_after_planner_continues_to_step_dispatch_when_mode_is_missing() -> None:
    assert route_after_planner(_state(None)) == "step_dispatch"


# --- no-plan guard -------------------------------------------------------
# A planner that produced no mission_spec used to fall through to step_dispatch,
# which relayed to the coder anyway; the coder's own missing-spec guard then
# raised an error that rode alongside the planner's real one and obscured it.


def test_route_after_planner_ends_the_turn_when_no_plan_was_produced() -> None:
    no_plan = {"task_id": "t1", "project_id": "p1", "session_permission_mode": "DEFAULT"}
    assert route_after_planner(no_plan) == END


def test_planner_dispatch_exit_ends_the_turn_when_no_plan_was_produced() -> None:
    """The dispatch-enabled planner exit must agree with route_after_planner."""
    from brain.engine import _route_planner_dispatch

    no_plan = {"task_id": "t1", "project_id": "p1", "session_permission_mode": "DEFAULT"}
    assert _route_planner_dispatch(no_plan) == END


def test_route_to_coders_dispatches_nothing_without_a_plan() -> None:
    from brain.engine import route_to_coders

    state = {"task_id": "t1", "project_id": "p1", "mission_spec": None}
    assert route_to_coders(cast(AIlienantGraphState, state)) == []
