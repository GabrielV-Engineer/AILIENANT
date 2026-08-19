# ailienant-core/tests/test_ideation.py
#
# Phase 2.21 DoD: pytest tests/test_ideation.py -v → 0 failures.
#
# Coverage:
#   run_analyst_node (async):
#     1. First round (no prior exchange) → asks a batch, records it in messages
#     2. Empty batch (the model's completion signal) → shared_understanding_reached
#     3. Free-text agreement response → shared_understanding_reached=True
#   route_after_analyst:
#     4. shared_understanding_reached=True → "synthesis_node"
#     5. shared_understanding_reached=False → "analyst_grill" (another round)
#   run_synthesis_node (async):
#     6. Distills a planner brief + handoff flags (never a MissionSpecification)

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


def _seam() -> Any:
    """Config carrying a clarification seam that answers every question with its
    first option — the node now suspends on native interrupt(), which needs a
    live runnable context, so tests inject this instead."""

    async def _fn(question_dicts: Any) -> Dict[str, Any]:
        return {
            "answers": [
                {
                    "id": q["id"],
                    "selected_labels": [q["options"][0]["label"]] if q["options"] else [],
                    "free_text": None,
                }
                for q in question_dicts
            ]
        }

    return {"configurable": {"analyst_clarification_fn": _fn}}


async def _run_both_phases(state: Dict[str, Any]) -> Dict[str, Any]:
    """Drives generate then, if a batch was actually committed, ask — mirrors
    brain/ideation.py's analyst_grill self-loop, which is now two graph
    super-steps per round (see agents/analyst.py::run_analyst_node's
    docstring for why generating and interrupting can't share one invocation).
    Returns the deltas merged, `messages` concatenated (append reducer)."""
    generate_delta = await run_analyst_node(state, {})
    if not generate_delta.get("pending_grill_batch"):
        return generate_delta
    ask_delta = await run_analyst_node({**state, **generate_delta}, _seam())
    return {
        **generate_delta,
        **ask_delta,
        "messages": generate_delta.get("messages", []) + ask_delta.get("messages", []),
    }


@pytest.mark.anyio
async def test_analyst_first_round_asks_a_batch(_force_debug: None) -> None:
    """First Socratic round: no prior exchange → asks a batch, records it, and
    stays unfinished so ideation's self-loop drives another round."""
    state = {"task_id": "test-sess", "user_input": "Build me a REST API", "messages": []}
    result = await _run_both_phases(state)
    assert result.get("shared_understanding_reached") is not True
    assert result.get("grill_round_count") == 1
    assert result.get("pending_grill_batch") is None, "the ask phase must clear it"
    assert any(m.get("role") == "assistant" for m in result.get("messages", []))


@pytest.mark.anyio
async def test_analyst_generate_phase_alone_commits_without_a_runnable_context(
    _force_debug: None,
) -> None:
    """The generate phase must not need a live graph context at all — it
    commits pending_grill_batch and returns; no config is required because it
    never calls the clarification seam."""
    state = {"task_id": "test-sess", "user_input": "Build me a REST API", "messages": []}
    result = await run_analyst_node(state)
    assert result.get("pending_grill_batch"), "expected a committed batch"
    assert result.get("messages", []) == []


@pytest.mark.anyio
async def test_analyst_second_round_completes_on_empty_batch(_force_debug: None) -> None:
    """The DEBUG stub returns an empty batch on any round after the first —
    the model's completion signal — so the analyst hands off."""
    state = {
        "task_id": "test-sess",
        "user_input": "I need it to handle 1000 RPS with <50ms p99",
        "messages": [{"role": "assistant", "content": "What is the primary deliverable?"}],
        "grill_round_count": 1,
    }
    result = await _run_both_phases(state)
    assert result.get("shared_understanding_reached") is True
    assert result.get("hitl_pending") is not True


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


def test_route_after_analyst_loops_for_another_round_when_not_reached() -> None:
    """The pause for the human's answers happens inside the node (interrupt()),
    so this edge loops back for another batch instead of ending the run."""
    assert route_after_analyst({"shared_understanding_reached": False}) == "analyst_grill"


def test_route_after_analyst_ends_on_hitl_pending_instead_of_looping() -> None:
    """Loop-safety guard: the degraded path (no reachable model) sets
    hitl_pending, which MUST win over the self-loop — otherwise the graph would
    retry a dead model until its recursion limit."""
    assert route_after_analyst(
        {"hitl_pending": True, "shared_understanding_reached": False}
    ) == END


@pytest.mark.anyio
async def test_analyst_degrades_to_actionable_notice_when_model_unreachable() -> None:
    """A None batch (model unreachable / never valid) must surface the BYOM
    notice and end the turn — never silently hand off an un-interviewed brief,
    and never self-loop."""
    called = False

    async def _never(_q: Any) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {}

    state = {"task_id": "test-sess", "user_input": "Build a REST API", "messages": []}

    with patch.object(analyst_mod, "DEBUG_MODE", False), patch(
        "agents.analyst._generate_grill_questions_llm", new=AsyncMock(return_value=None)
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_token", new=AsyncMock()
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_stream_end", new=AsyncMock()
    ):
        result = await run_analyst_node(
            state, {"configurable": {"analyst_clarification_fn": _never}}
        )

    assert result["hitl_pending"] is True
    assert result["shared_understanding_reached"] is False
    assert called is False, "a dead model must not reach the clarification suspend"
    assert route_after_analyst(result) == END, "the degraded turn must not self-loop"


@pytest.mark.anyio
async def test_grill_skips_the_llm_entirely_without_a_byom_target() -> None:
    """No active BYOM preset must fail fast rather than falling through to the
    litellm proxy and burning the transport-retry budget."""
    from agents.analyst import _generate_grill_questions_llm

    with patch("core.config.model_resolver.get_chat_target", return_value=None), patch(
        "tools.llm_gateway.LLMGateway.ainvoke", new=AsyncMock()
    ) as ainvoke:
        batch = await _generate_grill_questions_llm([], "SOUL", "", "sess")

    assert batch is None
    ainvoke.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# DEBT-181: a long grill must not silently forget history StateSummarizer
# compacted into a role="system" "[HISTORY SUMMARY]: ..." entry. Both replay
# sites (_build_grill_llm_messages, _dialogue_transcript) used to filter to
# role in ("user", "assistant") only, dropping it.
# ---------------------------------------------------------------------------

_HISTORY_SUMMARY = (
    "[HISTORY SUMMARY]: earlier the user agreed on JWT auth in src/auth/service.py."
)


def test_dialogue_transcript_includes_the_compacted_summary() -> None:
    from brain.ideation import _dialogue_transcript

    messages = [
        {"role": "system", "content": _HISTORY_SUMMARY},
        {"role": "assistant", "content": "What auth scheme?"},
        {"role": "user", "content": "JWT."},
    ]
    text = _dialogue_transcript(messages)
    assert "earlier the user agreed on JWT auth" in text
    assert "ANALYST: What auth scheme?" in text
    assert "USER: JWT." in text


def test_grill_llm_messages_fold_the_summary_into_the_system_prompt() -> None:
    from agents.analyst import _build_grill_llm_messages

    messages = [
        {"role": "system", "content": _HISTORY_SUMMARY},
        {"role": "assistant", "content": "What auth scheme?"},
        {"role": "user", "content": "JWT."},
    ]
    sent = _build_grill_llm_messages(messages, "SOUL", "")

    system_turns = [m for m in sent if m["role"] == "system"]
    assert len(system_turns) == 1  # folded, not duplicated as a second system turn
    assert "earlier the user agreed on JWT auth" in system_turns[0]["content"]
    # The prior Q&A is still replayed so the analyst never repeats itself.
    assert {"role": "assistant", "content": "What auth scheme?"} in sent
    assert {"role": "user", "content": "JWT."} in sent
