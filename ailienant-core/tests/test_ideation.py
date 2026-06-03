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

from typing import Any, Dict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

import agents.analyst as analyst_mod
import brain.ideation as ideation_mod
from brain.ideation import route_after_analyst, run_synthesis_node
from agents.analyst import run_analyst_node
from brain.state import MissionSpecification, WBSStep


@pytest.fixture
def _force_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deterministic synthetic path for the flow tests.

    The live path streams from a real BYOM model; these cases assert the
    suspend/agreement state contract, which is identical on both paths, so the
    stub keeps them hermetic without a network/model dependency.
    """
    monkeypatch.setattr(analyst_mod, "DEBUG_MODE", True)
    monkeypatch.setattr(ideation_mod, "DEBUG_MODE", True)


# ---------------------------------------------------------------------------
# run_analyst_node — HITL suspension tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_analyst_first_turn_suspends_with_question(_force_debug: None) -> None:
    """First Socratic turn: no prior exchange → sends question, sets hitl_pending=True."""
    state = {"task_id": "test-sess", "user_input": "Build me a REST API", "messages": []}
    result = await run_analyst_node(state)
    assert result.get("hitl_pending") is True
    assert result.get("shared_understanding_reached") is not True
    assert any(m.get("role") == "assistant" for m in result.get("messages", []))


@pytest.mark.anyio
async def test_analyst_non_agreement_response_continues_grilling(_force_debug: None) -> None:
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
async def test_synthesis_node_creates_mission_spec_and_disables_planner_mode(
    _force_debug: None,
) -> None:
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


# ---------------------------------------------------------------------------
# run_synthesis_node — live structured synthesis (the empty-plan defect fix)
# ---------------------------------------------------------------------------


def _llm_json(payload: Dict[str, Any]) -> Any:
    """Minimal litellm ModelResponse stand-in carrying a JSON string body."""
    import json
    body = json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=body))]
    )


@pytest.mark.anyio
async def test_synthesis_extracts_a_concrete_wbs_from_dialogue() -> None:
    """Live synthesis must yield a MissionSpecification with a real WBS — the
    empty-tasks placeholder is what produced the 'no concrete edits' regression."""
    plan_json: Dict[str, Any] = {
        "outcome": "A JWT auth service.",
        "scope": ["src/auth/service.py"],
        "constraints": ["No new external deps."],
        "decisions": ["Use PyJWT."],
        "tasks": [
            {
                "step_number": 1,
                "target_role": "core_dev",
                "action": "edit_file",
                "target_file": "src/auth/service.py",
                "description": "Add the token-issuing endpoint.",
            }
        ],
        "checks": ["Pytest exits 0."],
        "ubiquitous_language": {"token": "a signed JWT"},
        "tdd_criteria": ["A valid login returns a 200 with a token."],
    }
    state = {
        "task_id": "synth-sess",
        "messages": [
            {"role": "assistant", "content": "What auth scheme?"},
            {"role": "user", "content": "JWT, in src/auth/service.py."},
        ],
    }
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_llm_json(plan_json)),
    ):
        result = await run_synthesis_node(state)

    mission = result["mission_spec"]
    assert isinstance(mission, MissionSpecification)
    assert len(mission.tasks) >= 1
    assert all(isinstance(t, WBSStep) for t in mission.tasks)
    assert mission.tasks[0].target_file == "src/auth/service.py"
    assert result["planner_mode_active"] is False
    assert result.get("shared_understanding_reached") is True


@pytest.mark.anyio
async def test_synthesis_falls_back_honestly_on_bad_llm_output() -> None:
    """A malformed LLM payload degrades to an honest empty-WBS plan, never raises."""
    state: Dict[str, Any] = {"task_id": "synth-sess", "messages": []}
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_llm_json({"not": "a valid mission"})),
    ):
        result = await run_synthesis_node(state)
    mission = result["mission_spec"]
    assert isinstance(mission, MissionSpecification)
    assert mission.tasks == []          # honest: no concrete edits
    assert result["planner_mode_active"] is False
