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
# run_synthesis_node — distillation + handoff (NOT a plan)
# ---------------------------------------------------------------------------
#
# synthesis_node no longer drafts a MissionSpecification. It distills the dialogue
# into a planner brief, folds it into user_input, and flags ideation_synthesized so
# the parent graph routes the turn into the Actor-Critic planner. mission_spec is
# left for the planner to own — drafting it here in one zero-shot call was the
# single P(E) failure point the architecture review rejected.


def _llm_json(payload: Dict[str, Any]) -> Any:
    """Minimal litellm ModelResponse stand-in carrying a JSON string body."""
    import json
    body = json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=body))]
    )


@pytest.mark.anyio
async def test_synthesis_hands_off_brief_and_flags_planner(_force_debug: None) -> None:
    """DEBUG path: synthesis sets the handoff flags and never drafts a plan."""
    state = {
        "messages": [
            {"role": "assistant", "content": "What is the primary deliverable?"},
            {"role": "user", "content": "A working auth service with JWT."},
        ],
        "user_input": "looks good",
    }
    result = await run_synthesis_node(state)
    assert result.get("ideation_synthesized") is True
    assert result.get("planner_mode_active") is False
    assert result.get("shared_understanding_reached") is True
    # The planner owns the plan — synthesis must not emit a mission_spec.
    assert "mission_spec" not in result
    assert result.get("user_input")  # a brief was folded in for the planner


@pytest.mark.anyio
async def test_synthesis_distills_brief_into_planner_input() -> None:
    """Live distillation folds intent + constraints into user_input for the planner."""
    brief_json: Dict[str, Any] = {
        "intent": "Build a JWT auth service in src/auth/service.py.",
        "constraints": ["No new external deps."],
        "scope_hints": ["src/auth/service.py"],
        "ubiquitous_language": {"token": "a signed JWT"},
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
        new=AsyncMock(return_value=_llm_json(brief_json)),
    ):
        result = await run_synthesis_node(state)

    assert result.get("ideation_synthesized") is True
    assert "mission_spec" not in result
    brief = result["user_input"]
    assert "JWT auth service" in brief
    assert "No new external deps." in brief            # constraint folded in
    assert result["ideation_glossary"] == {"token": "a signed JWT"}


@pytest.mark.anyio
async def test_synthesis_degrades_to_raw_intent_on_bad_llm_output() -> None:
    """A malformed distillation degrades to a raw-intent brief and still hands off."""
    state: Dict[str, Any] = {
        "task_id": "synth-sess",
        "messages": [{"role": "user", "content": "Build a thing."}],
    }
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=RuntimeError("model down")),
    ):
        result = await run_synthesis_node(state)
    assert result.get("ideation_synthesized") is True
    assert "mission_spec" not in result
    assert "Build a thing." in result["user_input"]   # raw intent survived


# ---------------------------------------------------------------------------
# route_after_ideation — the handoff edge
# ---------------------------------------------------------------------------


def test_route_after_ideation_suspends_while_grilling() -> None:
    from brain.engine import route_after_ideation
    assert route_after_ideation({"hitl_pending": True}) == END


def test_route_after_ideation_hands_off_to_planner_after_synthesis() -> None:
    from brain.engine import route_after_ideation
    assert route_after_ideation({"ideation_synthesized": True}) == "planner_agent"


def test_route_after_ideation_defaults_to_end() -> None:
    from brain.engine import route_after_ideation
    assert route_after_ideation({}) == END
