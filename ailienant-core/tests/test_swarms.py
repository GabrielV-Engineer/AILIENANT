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
#     5. tci > 80 → parallel_tasks has ≥2 WBSStep entries
#
#   Full chain (async):
#     6. planner(tci=90) → route_to_coders → ≥2 Sends for SWARM (MANUAL_PLANNING=False path)

import pytest
from langgraph.constants import Send

from brain.engine import route_to_coders, route_after_summarize
from brain.state import WBSStep
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
    sends = route_to_coders(state)
    assert len(sends) >= 2, f"Expected ≥2 Sends for SWARM mode, got {len(sends)}"
    assert all(isinstance(s, Send) for s in sends), "All entries must be Send objects"


# ---------------------------------------------------------------------------
# run_planner_node — async planner fan-out tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_planner_yields_parallel_tasks_for_high_tci() -> None:
    """DEBUG_MODE=True, tci>80 → parallel_tasks contains ≥2 WBSSteps."""
    state = {"tci": 90.0, "css": 100.0}
    result = await run_planner_node(state)
    assert len(result["parallel_tasks"]) >= 2, (
        f"Expected ≥2 parallel tasks for tci=90, got {len(result['parallel_tasks'])}"
    )


@pytest.mark.anyio
async def test_planner_plus_router_swarm_chain_emits_sends() -> None:
    """Full MANUAL_PLANNING=False chain: planner(tci=90) → route_to_coders → ≥2 Sends."""
    planner_result = await run_planner_node({"tci": 90.0, "css": 100.0})

    router_state = {
        "provider": "CLOUD",
        "parallel_tasks": planner_result["parallel_tasks"],
        "mission_spec": planner_result["mission_spec"],
    }
    sends = route_to_coders(router_state)
    assert len(sends) >= 2, (
        f"MANUAL_PLANNING=False chain must yield ≥2 Sends; got {len(sends)}"
    )
