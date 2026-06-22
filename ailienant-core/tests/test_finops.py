# ailienant-core/tests/test_finops.py
#
# Phase 2.18 DoD: pytest tests/test_finops.py -v → 0 failures.
#
# Coverage:
#   run_finops_node (async): the budget gate now suspends via native interrupt()
#   (request_graph_approval) instead of a wall-clock-bounded asyncio.Event, so there is
#   no timeout branch — a resume always carries a decision.
#     1. Budget within ceiling → pass-through, approval never requested
#     2. Budget exceeded → request_graph_approval called exactly once → approved
#     3. Budget exceeded, user rejects → budget_rejected + errors
#
#   route_after_finops (sync): unchanged; "budget_rejected" → END, others → apply_patch.

from unittest.mock import patch

import pytest

from brain.finops import run_finops_node, route_after_finops


# ---------------------------------------------------------------------------
# run_finops_node — async node tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_finops_pass_through_when_within_budget() -> None:
    """cost <= max_budget → empty dict returned; the approval must never fire."""
    state = {"task_id": "t1", "current_cost_usd": 0.50, "max_budget_usd": 10.00}
    with patch(
        "core.hitl.request_graph_approval",
        side_effect=AssertionError("approval must NOT be requested within budget"),
    ):
        result = await run_finops_node(state)
    assert result == {}


@pytest.mark.anyio
async def test_finops_triggers_hitl_when_budget_exceeded() -> None:
    """cost > max_budget → request_graph_approval called exactly once; approve continues."""
    state = {"task_id": "t2", "current_cost_usd": 15.00, "max_budget_usd": 10.00}
    with patch(
        "core.hitl.request_graph_approval", return_value={"approved": True, "comment": None}
    ) as approve:
        result = await run_finops_node(state)
    approve.assert_called_once()
    assert result["hitl_response"] == "approved"
    assert result["hitl_pending"] is False


@pytest.mark.anyio
async def test_finops_rejected_returns_budget_rejected_and_errors() -> None:
    """Rejection → hitl_response='budget_rejected', errors list non-empty."""
    state = {"task_id": "t3", "current_cost_usd": 20.00, "max_budget_usd": 5.00}
    with patch(
        "core.hitl.request_graph_approval",
        return_value={"approved": False, "comment": "Stop, budget exhausted."},
    ):
        result = await run_finops_node(state)
    assert result["hitl_response"] == "budget_rejected", (
        f"Expected 'budget_rejected', got {result.get('hitl_response')!r}"
    )
    assert result["hitl_pending"] is False
    assert len(result.get("errors", [])) > 0, "errors list must be non-empty on rejection"


# ---------------------------------------------------------------------------
# route_after_finops — synchronous routing tests
# ---------------------------------------------------------------------------


def test_route_budget_rejected_goes_to_end() -> None:
    assert route_after_finops({"hitl_response": "budget_rejected"}) == "__end__"


def test_route_budget_timeout_goes_to_end() -> None:
    assert route_after_finops({"hitl_response": "budget_timeout"}) == "__end__"


def test_route_approved_goes_to_apply_patch() -> None:
    assert route_after_finops({"hitl_response": "approved"}) == "apply_patch"


def test_route_none_goes_to_apply_patch() -> None:
    """None = budget was OK; run_finops_node returned {} without touching hitl_response."""
    assert route_after_finops({"hitl_response": None}) == "apply_patch"


def test_route_does_not_collide_with_drift_monitor_values() -> None:
    """Regression guard: drift_monitor values must NOT trigger finops END routing.

    drift_monitor sets hitl_response to "rejected", "timeout", or "approved".
    None of these should be confused with the finops-namespaced "budget_rejected"
    / "budget_timeout" — this test would fail if someone accidentally uses
    plain "rejected" instead of "budget_rejected" in run_finops_node.
    """
    for drift_val in ("rejected", "timeout", "approved"):
        result = route_after_finops({"hitl_response": drift_val})
        assert result == "apply_patch", (
            f"drift_monitor value '{drift_val}' must not trigger finops END. Got: {result!r}"
        )
