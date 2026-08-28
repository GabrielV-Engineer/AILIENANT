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
from langchain_core.runnables import RunnableConfig
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


def _accept(_brief: str) -> Any:
    """A brief_review_fn double that accepts the draft unchanged."""
    async def _fn(brief_text: str) -> Dict[str, Any]:
        return {"approved": True, "comment": None, "modified_content": None}
    return _fn


async def _drive_synthesis(
    state: Dict[str, Any], review: Any = None, max_visits: int = 6
) -> Dict[str, Any]:
    """Run synthesis_node to a terminal decision, applying each delta to `state`.

    The node is two super-steps (draft, then review) driven by a self-loop, so a
    single call only ever produces the draft. `review` is the brief_review_fn seam;
    it defaults to accepting, which is what the pre-review tests asserted.
    """
    config: RunnableConfig = {"configurable": {"brief_review_fn": review or _accept("")}}
    result: Dict[str, Any] = {}
    for _ in range(max_visits):
        result = await run_synthesis_node(state, config)
        state.update(result)
        if not (state.get("pending_brief") or state.get("brief_revision_note")):
            break
    return result


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
    result = await _drive_synthesis(state)
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
        result = await _drive_synthesis(state)

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
        result = await _drive_synthesis(state)
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


# ---------------------------------------------------------------------------
# Brief review — the distillation is the one lossy step nothing else checked.
# The node is two super-steps so a resume never re-runs the MODEL_BIG draft.
# ---------------------------------------------------------------------------


def _brief_json() -> Dict[str, Any]:
    return {
        "intent": "Build a JWT auth service in src/auth/service.py.",
        "constraints": ["No new external deps.", "p99 under 50ms."],
        "scope_hints": ["src/auth/service.py"],
        "ubiquitous_language": {"token": "a signed JWT"},
    }


def _review_state() -> Dict[str, Any]:
    return {
        "task_id": "brief-sess",
        "messages": [
            {"role": "assistant", "content": "What auth scheme?"},
            {"role": "user", "content": "JWT, in src/auth/service.py."},
        ],
    }


@pytest.mark.anyio
async def test_draft_phase_commits_the_brief_without_suspending() -> None:
    """The first super-step must NOT interrupt: it only stages the brief.

    No brief_review_fn is injected, so reaching the review path would raise
    (native interrupt() outside a runnable context) — the absence of that error
    is the assertion.
    """
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_llm_json(_brief_json())),
    ):
        result = await run_synthesis_node(_review_state())

    pending = result.get("pending_brief")
    assert pending, "the draft phase must stage the brief for review"
    assert "ideation_synthesized" not in result, "nothing may hand off before review"
    assert "user_input" not in result
    # The composed brief carries the settled constraints as their own labelled
    # block, so rendering it verbatim shows them as a list — an omitted one is
    # invisible inside a paragraph, which is the whole point of the review.
    assert "No new external deps." in pending["composed"]
    assert "p99 under 50ms." in pending["composed"]
    assert pending["glossary"] == {"token": "a signed JWT"}


@pytest.mark.anyio
async def test_review_cycle_distils_exactly_once() -> None:
    """Draft -> review -> accept must cost ONE distillation, not one per replay.

    LangGraph replays a node from the top on every resume, so a single-phase
    implementation would re-run the MODEL_BIG call here — charging again and
    swapping out the very text the operator just read. A one-shot test cannot
    see that; driving both super-steps can.
    """
    invoke = AsyncMock(return_value=_llm_json(_brief_json()))
    state = _review_state()
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=invoke):
        result = await _drive_synthesis(state)

    assert invoke.await_count == 1, "the distillation ran more than once"
    assert result.get("ideation_synthesized") is True
    assert "No new external deps." in result["user_input"]
    assert result.get("pending_brief") is None


@pytest.mark.anyio
async def test_accepting_an_edited_brief_hands_off_the_edit() -> None:
    """What the operator read and corrected is what the planner must receive."""
    async def _edit(_brief_text: str) -> Dict[str, Any]:
        return {"approved": True, "comment": None, "modified_content": "Rewritten by hand."}

    state = _review_state()
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_llm_json(_brief_json())),
    ):
        result = await _drive_synthesis(state, review=_edit)

    assert result["user_input"] == "Rewritten by hand."
    assert result.get("ideation_synthesized") is True


@pytest.mark.anyio
async def test_rewrite_resteers_the_same_dialogue_without_touching_the_system_prompt() -> None:
    """A rejection carries the note into the NEXT draft's user payload only.

    The system message must stay byte-identical across drafts, and the rewrite
    must re-distil the same dialogue rather than re-entering the grill.
    """
    calls: list[Dict[str, Any]] = []

    async def _capture(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _llm_json(_brief_json())

    decisions: Any = iter([
        {"approved": False, "comment": "You dropped the latency constraint.", "modified_content": None},
        {"approved": True, "comment": None, "modified_content": None},
    ])

    async def _review(_brief_text: str) -> Dict[str, Any]:
        return next(decisions)

    state = _review_state()
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=_capture):
        result = await _drive_synthesis(state, review=_review)

    assert len(calls) == 2, "the rewrite must re-distil, not reuse the first draft"
    first_system = calls[0]["messages"][0]["content"]
    second_system = calls[1]["messages"][0]["content"]
    assert first_system == second_system, "the system prompt must stay byte-identical"
    assert "You dropped the latency constraint." in calls[1]["messages"][1]["content"]
    assert "You dropped the latency constraint." not in calls[0]["messages"][1]["content"]
    assert result.get("ideation_synthesized") is True
    assert result.get("brief_revision_note") is None, "the note is consumed, not carried"


@pytest.mark.anyio
async def test_cancelling_the_review_suspends_instead_of_faking_a_planner_failure() -> None:
    """Cancel must reach END through the suspend path, not the no-op dead end.

    route_after_ideation checks hitl_pending FIRST; without it the turn would be
    reported to the user as a planner failure the planner never had.
    """
    async def _cancel(_brief_text: str) -> Dict[str, Any]:
        return {"approved": False, "comment": None, "modified_content": None}

    state = _review_state()
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_llm_json(_brief_json())),
    ):
        result = await _drive_synthesis(state, review=_cancel)

    assert result.get("hitl_pending") is True
    assert result.get("ideation_synthesized") is not True
    assert state["messages"], "the dialogue survives so the next turn continues it"

    from brain.engine import route_after_ideation
    assert route_after_ideation(state) == END


# ---------------------------------------------------------------------------
# route_after_synthesis — the self-loop that drives the two phases
# ---------------------------------------------------------------------------


def test_route_after_synthesis_revisits_for_the_review_phase() -> None:
    from brain.ideation import route_after_synthesis
    assert route_after_synthesis({"pending_brief": {"composed": "x"}}) == "synthesis_node"


def test_route_after_synthesis_revisits_for_a_rewrite() -> None:
    from brain.ideation import route_after_synthesis
    assert route_after_synthesis({"brief_revision_note": "add latency"}) == "synthesis_node"


def test_route_after_synthesis_ends_once_accepted() -> None:
    from brain.ideation import route_after_synthesis
    assert route_after_synthesis({"ideation_synthesized": True}) == END


def test_route_after_synthesis_checks_suspend_before_the_self_loop() -> None:
    """Load-bearing ordering: a suspend with a brief still staged must not spin."""
    from brain.ideation import route_after_synthesis
    assert route_after_synthesis({"hitl_pending": True, "pending_brief": {"composed": "x"}}) == END
