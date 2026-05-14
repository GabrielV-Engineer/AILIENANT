# ailienant-core/tests/test_ideation.py
#
# Phase 2.21 DoD: pytest tests/test_ideation.py -v → 0 failures.
#
# Coverage:
#   run_analyst_node (async):
#     1. First turn (no prior exchange) → hitl_pending=True, question in messages
#     2. Non-agreement response → hitl_pending=True (next question asked)
#     3. Agreement response → shared_understanding_reached=True
#   route_after_analyst:
#     4. shared_understanding_reached=True → "synthesis_node"
#     5. shared_understanding_reached=False → END
#   run_synthesis_node (async):
#     6. Produces MissionSpecification, planner_mode_active=False

import pytest
from langgraph.graph import END

from brain.ideation import route_after_analyst, run_synthesis_node
from agents.analyst import run_analyst_node
from brain.state import MissionSpecification


# ---------------------------------------------------------------------------
# run_analyst_node — HITL suspension tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_analyst_first_turn_suspends_with_question() -> None:
    """First Socratic turn: no prior exchange → sends question, sets hitl_pending=True."""
    state = {"task_id": "test-sess", "user_input": "Build me a REST API", "messages": []}
    result = await run_analyst_node(state)
    assert result.get("hitl_pending") is True
    assert result.get("shared_understanding_reached") is not True
    assert any(m.get("role") == "assistant" for m in result.get("messages", []))


@pytest.mark.anyio
async def test_analyst_non_agreement_response_continues_grilling() -> None:
    """Human answers without agreement → analyst asks next question, hitl_pending=True."""
    prior = [{"role": "assistant", "content": "What is the primary deliverable?"}]
    state = {
        "task_id": "test-sess",
        "user_input": "I need it to handle 1000 RPS with <50ms p99",
        "messages": prior,
    }
    result = await run_analyst_node(state)
    assert result.get("hitl_pending") is True
    assert result.get("shared_understanding_reached") is not True


@pytest.mark.anyio
async def test_analyst_agreement_response_sets_shared_understanding() -> None:
    """Human signals agreement → shared_understanding_reached=True, hitl_pending=False."""
    prior = [{"role": "assistant", "content": "Does this plan look solid?"}]
    state = {
        "task_id": "test-sess",
        "user_input": "looks good, let's proceed",
        "messages": prior,
    }
    result = await run_analyst_node(state)
    assert result.get("shared_understanding_reached") is True
    assert result.get("hitl_pending") is not True


# ---------------------------------------------------------------------------
# route_after_analyst — routing logic tests
# ---------------------------------------------------------------------------


def test_route_after_analyst_goes_to_synthesis_when_understanding_reached() -> None:
    assert route_after_analyst({"shared_understanding_reached": True}) == "synthesis_node"


def test_route_after_analyst_suspends_to_end_when_not_reached() -> None:
    assert route_after_analyst({"shared_understanding_reached": False}) == END


# ---------------------------------------------------------------------------
# run_synthesis_node — compression test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_synthesis_node_creates_mission_spec_and_disables_planner_mode() -> None:
    """SynthesisNode produces MissionSpecification and sets planner_mode_active=False."""
    state = {
        "messages": [
            {"role": "assistant", "content": "What is the primary deliverable?"},
            {"role": "user", "content": "A working auth service with JWT."},
        ],
        "user_input": "looks good",
    }
    result = await run_synthesis_node(state)
    assert isinstance(result["mission_spec"], MissionSpecification)
    assert result["planner_mode_active"] is False
    assert result.get("shared_understanding_reached") is True
