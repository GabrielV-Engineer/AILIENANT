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

from typing import Any, Dict

from langgraph.graph import END

from brain.engine import route_after_planner


def _state(session_permission_mode: Any) -> Dict[str, Any]:
    return {"task_id": "t1", "project_id": "p1", "session_permission_mode": session_permission_mode}


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
