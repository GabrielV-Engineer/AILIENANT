# ailienant-core/tests/test_drift_monitor.py
#
# DoD: pytest tests/test_drift_monitor.py -v must pass with 0 failures.

from unittest.mock import patch

import pytest

from brain.drift_monitor import (
    _DRIFT_THRESHOLD,
    _plan_similarity,
    run_drift_compute_node,
    run_drift_gate_node,
)
from brain.state import MissionSpecification, WBSStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(outcome: str, descriptions: list[str]) -> MissionSpecification:
    tasks = [
        WBSStep(
            step_number=i + 1,
            target_role="Refactor",
            action="read_file",
            target_file=f"file_{i}.py",
            description=desc,
            status="pending",
        )
        for i, desc in enumerate(descriptions)
    ]
    return MissionSpecification(
        outcome=outcome,
        scope=["src/"],
        constraints=["No external libs"],
        decisions=["Use stdlib"],
        tasks=tasks,
        checks=["Tests pass"],
    )


# ---------------------------------------------------------------------------
# _plan_similarity — unit tests
# ---------------------------------------------------------------------------


def test_identical_plans_have_similarity_one() -> None:
    spec = _make_spec("Refactor auth module", ["Read auth.py", "Write auth.py"])
    assert _plan_similarity(spec, spec) == pytest.approx(1.0)


def test_completely_different_plans_have_low_similarity() -> None:
    a = _make_spec("Refactor auth module", ["Read auth.py", "Write tests"])
    b = _make_spec("Deploy Kubernetes cluster", ["Configure helm chart", "Run kubectl apply"])
    sim = _plan_similarity(a, b)
    assert sim < _DRIFT_THRESHOLD, f"Expected sim < {_DRIFT_THRESHOLD}, got {sim}"


# ---------------------------------------------------------------------------
# drift_compute / drift_gate — split-node tests (8.10.14)
#
# The gate's approval is native interrupt() (request_graph_approval); unit tests patch
# that seam to return the resume verdict directly, so they exercise the gate's delta
# logic without a live graph run. The real interrupt/resume round-trip is covered by
# tests/test_phase8_10_14_checkpoint_gate.py.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_drift_on_identical_plans() -> None:
    """Identical plans → drift_compute closes the gate; drift_gate is a no-op."""
    spec = _make_spec("Refactor auth module", ["Read auth.py", "Write tests"])
    state = {"immutable_wbs": spec, "mission_spec": spec, "task_id": "t1"}
    compute = await run_drift_compute_node(state)
    assert compute == {"drift_gate_open": False}
    assert await run_drift_gate_node({**state, **compute}) == {}


@pytest.mark.anyio
async def test_no_drift_on_first_turn() -> None:
    """First turn (immutable_wbs None) → gate closed unconditionally."""
    spec = _make_spec("Deploy service", ["Write Dockerfile"])
    state = {"immutable_wbs": None, "mission_spec": spec, "task_id": "t2"}
    assert await run_drift_compute_node(state) == {"drift_gate_open": False}


@pytest.mark.anyio
async def test_drift_compute_opens_gate_then_gate_approves() -> None:
    """Divergent plans → drift_compute opens the gate (committing the decision);
    drift_gate then suspends and, on approval, updates immutable_wbs."""
    baseline = _make_spec("Refactor auth module", ["Read auth.py", "Write tests for auth"])
    revised = _make_spec("Deploy Kubernetes cluster", ["Configure helm chart", "Run kubectl"])
    state = {"immutable_wbs": baseline, "mission_spec": revised, "task_id": "t3"}

    compute = await run_drift_compute_node(state)
    assert compute["drift_gate_open"] is True
    assert compute["drift_similarity"] < _DRIFT_THRESHOLD

    with patch(
        "core.hitl.request_graph_approval",
        return_value={"approved": True, "comment": "OK, proceed"},
    ):
        result = await run_drift_gate_node({**state, **compute})

    assert result.get("hitl_response") == "approved"
    assert result.get("immutable_wbs") == revised
    assert result.get("hitl_pending") is False


@pytest.mark.anyio
async def test_drift_gate_rejected_propagates_error() -> None:
    """On rejection the gate emits an error and does NOT update immutable_wbs."""
    baseline = _make_spec("Refactor auth module", ["Read auth.py", "Write tests for auth"])
    revised = _make_spec("Deploy Kubernetes cluster", ["Configure helm chart", "Run kubectl"])
    state = {"immutable_wbs": baseline, "mission_spec": revised, "task_id": "t4"}

    compute = await run_drift_compute_node(state)
    with patch(
        "core.hitl.request_graph_approval",
        return_value={"approved": False, "comment": "No, revert"},
    ):
        result = await run_drift_gate_node({**state, **compute})

    assert result.get("hitl_response") == "rejected"
    assert len(result.get("errors", [])) > 0
    assert "immutable_wbs" not in result


# ---------------------------------------------------------------------------
# PlannerAgent immutable_wbs freeze — integration-style test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_planner_freezes_immutable_wbs_on_first_turn() -> None:
    """run_planner_node must set immutable_wbs when state["immutable_wbs"] is None."""
    from agents.planner import run_planner_node

    state: dict = {
        "immutable_wbs": None,
        "tci": 0.0,
        "css": 100.0,
        "task_id": "t5",
    }
    with patch("agents.planner.DEBUG_MODE", True):
        result = await run_planner_node(state)

    assert "immutable_wbs" in result, "PlannerAgent must set immutable_wbs on first turn"
    assert result["immutable_wbs"] is not None
    assert result["immutable_wbs"] == result["mission_spec"], (
        "immutable_wbs must equal mission_spec on the first turn"
    )


@pytest.mark.anyio
async def test_planner_does_not_overwrite_immutable_wbs_on_retry() -> None:
    """run_planner_node must NOT update immutable_wbs when it is already set."""
    from agents.planner import run_planner_node

    existing_wbs = _make_spec("Original plan", ["Step one"])

    state: dict = {
        "immutable_wbs": existing_wbs,
        "tci": 0.0,
        "css": 100.0,
        "task_id": "t6",
    }
    with patch("agents.planner.DEBUG_MODE", True):
        result = await run_planner_node(state)

    # Either the key is absent (planner didn't touch it) or value unchanged
    if "immutable_wbs" in result:
        assert result["immutable_wbs"] == existing_wbs, (
            "immutable_wbs must NOT be overwritten by subsequent planner calls"
        )
