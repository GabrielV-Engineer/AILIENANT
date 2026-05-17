# tests/test_orchestrator.py
"""Phase 4.1.3 DoD — OrchestratorAgent: WBS lifecycle + Bounded Failure + Prompt Swap.

Six tests cover the four primary paths (happy step pick, bounded failure ceiling,
RED ALERT flag, all-complete terminal signal) plus the two risk-audit fixes:
  - R2 idempotency on step already 'in_progress'
  - R3 dict-shaped context_metrics deserialization
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from brain.state import ContextMeter, MissionSpecification, WBSStep


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_step(
    n: int,
    role: str = "Refactor",
    status: str = "pending",
    action: str = "read_file",
    target_file: str = "main.py",
    description: str = "Stub step.",
) -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role=role,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        target_file=target_file,
        description=description,
        status=status,  # type: ignore[arg-type]
    )


def _make_mission(tasks: List[WBSStep]) -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.",
        scope=["main.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=tasks,
        checks=["Pytest exits 0."],
    )


def _make_state(mission: MissionSpecification, **overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "orchestrator-test",
        "mission_spec": mission,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "context_metrics": None,
        "css": 80.0,
    }
    state.update(overrides)
    return state


# ── Test 1: happy path — pick first pending, emit target_role ────────────────


@pytest.mark.anyio
async def test_orchestrator_picks_first_pending_and_sets_target_role() -> None:
    tasks = [
        _make_step(1, role="Refactor", status="completed"),
        _make_step(2, role="Test", status="pending"),
        _make_step(3, role="Doc", status="pending"),
    ]
    state = _make_state(_make_mission(tasks))

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    assert result["current_step_id"] == 2
    assert result["target_role"] == "Test"

    # Mission was mutated: step 2 now in_progress; steps 1 and 3 unchanged.
    updated: MissionSpecification = result["mission_spec"]
    assert updated.tasks[0].status == "completed"
    assert updated.tasks[1].status == "in_progress"
    assert updated.tasks[2].status == "pending"

    # No bounded-failure / red-alert flags emitted on the happy path.
    flags: List[str] = result.get("security_flags", [])
    assert "BOUNDED_FAILURE_LIMIT_REACHED" not in flags
    assert "RED_ALERT_ORCHESTRATOR" not in flags
    assert result.get("hitl_pending") is None


# ── Test 2: bounded failure ceiling escalates to HITL ────────────────────────


@pytest.mark.anyio
async def test_orchestrator_bounded_failure_marks_step_failed_and_escalates_hitl() -> None:
    tasks = [_make_step(1, role="Infra", status="pending")]
    state = _make_state(_make_mission(tasks), retry_count=3)  # one over MAX_RETRIES=2

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    assert result["hitl_pending"] is True
    assert "BOUNDED_FAILURE_LIMIT_REACHED" in result["security_flags"]

    updated: MissionSpecification = result["mission_spec"]
    assert updated.tasks[0].status == "failed"

    # retry_count is reset so a subsequent HITL-unblocked step starts clean.
    assert result["retry_count"] == 0

    assert result["errors"], "Bounded failure must emit an errors entry."
    err = result["errors"][0]
    assert "step 1" in err
    assert "Infra" in err
    assert "3 retries" in err
    assert "cap=2" in err


# ── Test 3: RED ALERT emitted when css below threshold ────────────────────────


@pytest.mark.anyio
async def test_orchestrator_emits_red_alert_when_css_below_threshold() -> None:
    metrics = ContextMeter(
        semantic_similarity=0.3,
        graph_coverage=0.2,
        recency_score=0.4,
        css_total=30.0,
        task_complexity_index=50.0,
        routing_decision="LOCAL_SMALL",
        is_red_alert=True,
    )
    tasks = [_make_step(1, role="SecOps", status="pending")]
    state = _make_state(_make_mission(tasks), context_metrics=metrics, css=30.0)

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    assert "RED_ALERT_ORCHESTRATOR" in result["security_flags"]
    # RED ALERT is informational, not a halt.
    assert result["current_step_id"] == 1
    assert result["target_role"] == "SecOps"
    updated: MissionSpecification = result["mission_spec"]
    assert updated.tasks[0].status == "in_progress"


# ── Test 4: all-complete terminal signal ─────────────────────────────────────


@pytest.mark.anyio
async def test_orchestrator_signals_all_complete_when_no_pending_steps() -> None:
    tasks = [
        _make_step(1, role="Refactor", status="completed"),
        _make_step(2, role="Test", status="completed"),
    ]
    state = _make_state(_make_mission(tasks))

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    assert "ALL_WBS_STEPS_COMPLETE" in result["security_flags"]
    assert result["current_step_id"] is None
    assert result["target_role"] is None
    # No mission mutation emitted on the terminal path.
    assert "mission_spec" not in result
    assert not result.get("errors")


# ── Test 5 (R2): idempotent on in_progress ───────────────────────────────────


@pytest.mark.anyio
async def test_orchestrator_idempotent_when_step_already_in_progress() -> None:
    tasks = [_make_step(1, role="Doc", status="in_progress")]
    state = _make_state(_make_mission(tasks))

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    # Critical invariant: no mission_spec mutation when status was already in_progress.
    assert "mission_spec" not in result
    assert result["current_step_id"] == 1
    assert result["target_role"] == "Doc"


# ── Test 6 (R3): RED ALERT works with dict-shaped context_metrics ────────────


@pytest.mark.anyio
async def test_orchestrator_red_alert_works_with_dict_shaped_context_metrics() -> None:
    """Simulates LangGraph SQLite deserialization that emits dict instead of model."""
    dict_metrics = {
        "semantic_similarity": 0.2,
        "graph_coverage": 0.1,
        "recency_score": 0.3,
        "css_total": 25.0,
        "task_complexity_index": 60.0,
        "routing_decision": "LOCAL_SMALL",
        "is_red_alert": True,
    }
    tasks = [_make_step(1, role="Refactor", status="pending")]
    state = _make_state(_make_mission(tasks), context_metrics=dict_metrics, css=25.0)

    from agents.orchestrator import run_orchestrator_node

    result = await run_orchestrator_node(state)

    assert "RED_ALERT_ORCHESTRATOR" in result["security_flags"]
    assert result["current_step_id"] == 1


# ── Silence unused-import lint when MagicMock isn't referenced ───────────────
_ = MagicMock  # type: ignore[truthy-function]
