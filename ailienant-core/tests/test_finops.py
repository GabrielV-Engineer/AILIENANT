# ailienant-core/tests/test_finops.py
#
# Phase 2.18 DoD: pytest tests/test_finops.py -v → 0 failures.
#
# Coverage:
#   run_finops_node (async):
#     1. Budget within ceiling → pass-through, HITL never called
#     2. Budget exceeded → request_human_approval called exactly once
#     3. Budget exceeded, user rejects → budget_rejected + errors
#     4. Budget exceeded, HITL timeout → budget_timeout + errors (fail-safe)
#
#   route_after_finops (sync):
#     5. "budget_rejected" → "__end__"
#     6. "budget_timeout"  → "__end__"
#     7. "approved"        → "apply_patch"
#     8. None              → "apply_patch" (normal budget-OK path)
#     9. drift_monitor values ("rejected"/"timeout"/"approved") → "apply_patch" (collision guard)

from unittest.mock import AsyncMock, patch

import pytest

from api.websocket_manager import vfs_manager
from brain.finops import run_finops_node, route_after_finops


# ---------------------------------------------------------------------------
# run_finops_node — async node tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_finops_pass_through_when_within_budget() -> None:
    """cost <= max_budget → empty dict returned; HITL gate must never fire."""
    state = {"task_id": "t1", "current_cost_usd": 0.50, "max_budget_usd": 10.00}
    with patch.object(
        vfs_manager,
        "request_human_approval",
        new=AsyncMock(side_effect=AssertionError("HITL must NOT be called within budget")),
    ):
        result = await run_finops_node(state)
    assert result == {}


@pytest.mark.anyio
async def test_finops_triggers_hitl_when_budget_exceeded() -> None:
    """cost > max_budget → request_human_approval called exactly once."""
    state = {"task_id": "t2", "current_cost_usd": 15.00, "max_budget_usd": 10.00}
    mock_approval = AsyncMock(return_value={"approved": True})
    with patch.object(vfs_manager, "request_human_approval", new=mock_approval):
        await run_finops_node(state)
    mock_approval.assert_called_once()


@pytest.mark.anyio
async def test_finops_rejected_returns_budget_rejected_and_errors() -> None:
    """HITL rejection → hitl_response='budget_rejected', errors list non-empty."""
    state = {"task_id": "t3", "current_cost_usd": 20.00, "max_budget_usd": 5.00}
    with patch.object(
        vfs_manager,
        "request_human_approval",
        new=AsyncMock(return_value={"approved": False, "comment": "Stop, budget exhausted."}),
    ):
        result = await run_finops_node(state)
    assert result["hitl_response"] == "budget_rejected", (
        f"Expected 'budget_rejected', got {result.get('hitl_response')!r}"
    )
    assert result["hitl_pending"] is False
    assert len(result.get("errors", [])) > 0, "errors list must be non-empty on rejection"


@pytest.mark.anyio
async def test_finops_timeout_returns_budget_timeout_as_failsafe() -> None:
    """HITL timeout (None) → hitl_response='budget_timeout', errors non-empty (fail-safe)."""
    state = {"task_id": "t4", "current_cost_usd": 12.00, "max_budget_usd": 10.00}
    with patch.object(
        vfs_manager, "request_human_approval", new=AsyncMock(return_value=None)
    ):
        result = await run_finops_node(state)
    assert result["hitl_response"] == "budget_timeout", (
        f"Expected 'budget_timeout', got {result.get('hitl_response')!r}"
    )
    assert result["hitl_pending"] is False
    assert len(result.get("errors", [])) > 0, (
        "errors list must be non-empty on timeout so guardrail has context"
    )


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
