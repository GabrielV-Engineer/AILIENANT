# ailienant-core/tests/test_swarms.py
#
# Phase 2.19/2.20 DoD: pytest tests/test_swarms.py -v → 0 failures.
#
# Coverage:
#   route_after_summarize (sync):
#     1. planner_mode_active=False → "planner_agent"
#     2. planner_mode_active=True  → "ideation_loop"
#     3. field absent (falsy)      → "planner_agent" (safe default)
#
#   route_to_coders (sync):
#     4. CLOUD + parallel_tasks=[2 tasks] → list of ≥2 Send objects
#
#   run_planner_node (async, DEBUG_MODE=True):
#     5. tci > 80 → parallel_tasks is empty (sequential RELAY; no unsafe fan-out)
#
#   Full chain (async):
#     6. planner(tci=90) → route_to_coders → exactly 1 Send (sequential RELAY path)

from unittest.mock import patch

import pytest
from langgraph.constants import Send

from typing import cast

from brain.engine import route_to_coders, route_after_summarize
from brain.state import AIlienantGraphState, WBSStep
from agents.planner import run_planner_node


# ---------------------------------------------------------------------------
# route_after_summarize — synchronous routing tests
# ---------------------------------------------------------------------------


def test_route_after_summarize_goes_to_planner_when_not_planner_mode() -> None:
    assert route_after_summarize({"planner_mode_active": False}) == "planner_agent"


def test_route_after_summarize_goes_to_ideation_loop_when_planner_mode() -> None:
    assert route_after_summarize({"planner_mode_active": True}) == "ideation_loop"


def test_route_after_summarize_absent_field_defaults_to_planner() -> None:
    """Missing planner_mode_active → falsy → planner_agent (safe default)."""
    assert route_after_summarize({}) == "planner_agent"


# ---------------------------------------------------------------------------
# route_to_coders — MapReduce fan-out test
# ---------------------------------------------------------------------------


def test_route_to_coders_emits_multiple_sends_in_swarm_mode() -> None:
    """CLOUD provider + 2 parallel tasks → ≥2 Send objects."""
    tasks = [
        WBSStep(
            step_number=1,
            target_role="Refactor",
            action="read_file",
            target_file="main.py",
            description="Step A: read main.",
        ),
        WBSStep(
            step_number=2,
            target_role="Test",
            action="read_file",
            target_file="requirements.txt",
            description="Step B: audit deps.",
        ),
    ]
    state = {"provider": "CLOUD", "parallel_tasks": tasks, "mission_spec": None}
    sends = route_to_coders(cast(AIlienantGraphState, state))
    assert len(sends) >= 2, f"Expected ≥2 Sends for SWARM mode, got {len(sends)}"
    assert all(isinstance(s, Send) for s in sends), "All entries must be Send objects"


# ---------------------------------------------------------------------------
# route_to_coders — explicit per-step role augmentation on the Send payload
# ---------------------------------------------------------------------------


def test_swarm_send_carries_per_step_role() -> None:
    """Each fan-out Send payload carries its own step's role, not the task-initial
    role, so per-step tool selection is scoped to the step that runs there."""
    tasks = [
        WBSStep(
            step_number=1,
            target_role="secops",
            action="read_file",
            target_file="a.py",
            description="Step A.",
        ),
        WBSStep(
            step_number=2,
            target_role="qa_tester",
            action="read_file",
            target_file="b.py",
            description="Step B.",
        ),
    ]
    state = {
        "provider": "CLOUD",
        "parallel_tasks": tasks,
        "mission_spec": None,
        "active_role": "core_dev",  # task-initial role — must NOT leak onto the steps
    }
    sends = route_to_coders(cast(AIlienantGraphState, state))
    roles = {s.arg["active_role"] for s in sends}
    assert roles == {"secops", "qa_tester"}, (
        f"SWARM fan-out must carry per-step roles, not the task-initial role; got {roles}"
    )


def test_relay_send_carries_pending_step_role() -> None:
    """The single relayed Send is scoped to the first pending step's role, overriding
    whatever role the task entered with."""
    import types

    pending = types.SimpleNamespace(
        step_number=3, target_role="devops_infra", status="pending"
    )
    mission = types.SimpleNamespace(tasks=[pending])
    state = {
        "provider": "LOCAL",
        "parallel_tasks": [],
        "mission_spec": mission,
        "active_role": "core_dev",
    }
    sends = route_to_coders(cast(AIlienantGraphState, state))
    assert len(sends) == 1
    assert sends[0].arg["active_role"] == "devops_infra"


# ---------------------------------------------------------------------------
# run_planner_node — async planner fan-out tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_planner_sequential_for_high_tci() -> None:
    """High-TCI plans no longer fan out: WBS steps carry only implicit step_number
    ordering, so the planner yields no parallel_tasks and execution is sequential."""
    state = {"tci": 90.0, "css": 100.0}
    with patch("agents.planner.DEBUG_MODE", True):
        result = await run_planner_node(state)
    assert result["parallel_tasks"] == [], (
        f"High-TCI plans must execute sequentially; got {len(result['parallel_tasks'])} parallel tasks"
    )


@pytest.mark.anyio
async def test_planner_plus_router_yields_sequential_send() -> None:
    """Full chain: planner(tci=90) → route_to_coders now takes the sequential RELAY
    path (one Send), since the planner no longer populates parallel_tasks."""
    with patch("agents.planner.DEBUG_MODE", True):
        planner_result = await run_planner_node({"tci": 90.0, "css": 100.0})

    router_state = {
        "provider": "CLOUD",
        "parallel_tasks": planner_result["parallel_tasks"],
        "mission_spec": planner_result["mission_spec"],
    }
    sends = route_to_coders(cast(AIlienantGraphState, router_state))
    assert len(sends) == 1, (
        f"Sequential RELAY must yield exactly one Send; got {len(sends)}"
    )
