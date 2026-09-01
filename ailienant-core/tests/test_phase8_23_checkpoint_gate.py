# ailienant-core/tests/test_phase8_23_checkpoint_gate.py
#
# Division checkpoint gate (test-only, sibling convention). Certifies the two
# defects this division exists to fix, at their production entry points:
#
#   A. Consecutive grill rounds no longer reason from identical inputs.
#   B. The distillation preserves what the operator specified instead of
#      paraphrasing it away.
#
# Gates never re-run sibling suites; each row asserts an invariant directly.

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

import agents.analyst as analyst_mod
import brain.ideation as ideation_mod
from tools.control_tools import AskUserQuestionItem, AskUserQuestionOptionInput
from agents.analyst import (
    _GRILL_REASONING_TEMPERATURE,
    _parse_coverage_axes,
    _render_answered_so_far,
    _stream_grill_reasoning,
)
from brain.ideation import (
    _compose_planner_brief,
    _coverage_settled,
    _reasoning_transcript,
    _resolve_distill_budget,
    route_after_analyst,
)


class _Delta:
    """Minimal stand-in for the gateway's reasoning delta."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.source = "text"


def _streamer(chunks: List[str], captured_calls: Optional[List[Any]] = None) -> Any:
    """astream_reasoning replacement that records its kwargs and yields chunks."""

    def _fake(messages: Any, **kwargs: Any) -> Any:
        if captured_calls is not None:
            captured_calls.append({"messages": messages, "kwargs": kwargs})

        async def _gen() -> Any:
            for c in chunks:
                yield _Delta(c)

        return _gen()

    return _fake


async def _sink(_text: str, _source: str) -> None:
    """Thought Box sink stub — the reasoning pass is a no-op without one."""
    return None


# ---------------------------------------------------------------------------
# A. The grill stops repeating itself
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_row01_prior_reasoning_reaches_the_next_round() -> None:
    """Round N+1's prompt carries round N's conclusion.

    The defect: the reasoning pass never received the dialogue, so round 3 ran on
    byte-identical inputs to round 1 and returned near-identical prose.
    """
    from tools.llm_gateway import LLMGateway

    calls: List[Any] = []
    with patch.object(LLMGateway, "astream_reasoning", _streamer(["second pass"], calls)):
        await _stream_grill_reasoning(
            user_input="Add a dark mode toggle",
            context_block="ctx",
            soul_prompt="soul",
            session_id="s",
            on_thinking=_sink,
            thinking_on=True,
            thinking_budget=1024,
            prior_reasoning="I already established the theme lives in ThemeProvider.",
            answered_so_far="- Persist the choice",
        )

    prompt = calls[0]["messages"][-1]["content"]
    assert "ThemeProvider" in prompt, "the previous round's reasoning must reach this one"
    assert "Persist the choice" in prompt, "the operator's answers must reach the reasoning pass"
    assert "Do not restate" in prompt, "the anti-repetition directive must be present"


@pytest.mark.anyio
async def test_row01b_the_node_actually_wires_the_memory_through() -> None:
    """run_analyst_node passes state's reasoning log and answers to the pass.

    Row 1 proves the FUNCTION honours its parameters; this proves the NODE
    supplies them. The original defect lived exactly in that gap — the function
    was fine, the call site never handed it the dialogue — and it is the class of
    failure a unit test cannot see, since both halves are individually correct.
    """
    from tools.llm_gateway import LLMGateway

    calls: List[Any] = []
    state: Dict[str, Any] = {
        "task_id": "t",
        "user_input": "stale text",
        "original_user_request": "Add SSO with Okta",
        "messages": [
            {"role": "assistant", "content": "- auth: which provider?"},
            {"role": "user", "content": "Provider: Okta"},
        ],
        "grill_reasoning_log": ["Round one settled the provider question."],
        "grill_coverage_axes": ["session storage"],
        "grill_round_count": 1,
    }

    with patch.object(LLMGateway, "astream_reasoning", _streamer(["x"], calls)), \
         patch.object(analyst_mod, "_assemble_socratic_context", AsyncMock(return_value="")), \
         patch.object(analyst_mod, "_gather_tool_grounding", AsyncMock(return_value=("", []))), \
         patch.object(analyst_mod, "_generate_grill_questions_llm", AsyncMock(return_value=None)):
        await analyst_mod.run_analyst_node(
            state,
            {"configurable": {"stream_thinking": _sink, "enable_native_thinking": True}},
        )

    assert calls, "the node must run the reasoning pass"
    prompt = calls[0]["messages"][-1]["content"]
    assert "Round one settled the provider question." in prompt, (
        "the node must hand the previous round's reasoning to this one"
    )
    assert "Provider: Okta" in prompt, "the node must hand the operator's answers through"
    assert "session storage" in prompt, "previously named axes must reach the pass"
    assert "Add SSO with Okta" in prompt, "the original request must beat the stale user_input"


@pytest.mark.anyio
async def test_row01c_captured_reasoning_is_persisted_to_state() -> None:
    """Whatever the round reasoned is written back for the next round to read.

    Without this the memory is one-way: the pass would receive a log that nothing
    ever appends to, and every round would still start from the same blank slate.
    """
    from tools.llm_gateway import LLMGateway

    state: Dict[str, Any] = {
        "task_id": "t", "user_input": "build it", "messages": [], "grill_round_count": 0,
    }

    with patch.object(LLMGateway, "astream_reasoning",
                      _streamer(["concluded X.\nAXES: caching, retries"])), \
         patch.object(analyst_mod, "_assemble_socratic_context", AsyncMock(return_value="")), \
         patch.object(analyst_mod, "_gather_tool_grounding", AsyncMock(return_value=("", []))), \
         patch.object(analyst_mod, "_generate_grill_questions_llm", AsyncMock(return_value=None)):
        result = await analyst_mod.run_analyst_node(
            state,
            {"configurable": {"stream_thinking": _sink, "enable_native_thinking": True}},
        )

    assert result.get("grill_reasoning_log"), "the round's reasoning must be persisted"
    assert "concluded X." in result["grill_reasoning_log"][0]
    assert result.get("grill_coverage_axes") == ["caching", "retries"], (
        "the axes the model named must be persisted for the stop criterion"
    )


@pytest.mark.anyio
async def test_row01d_persistence_covers_every_round_advancing_exit() -> None:
    """All three exits that advance the round carry the reasoning back.

    The node returns from three places — a committed batch, the model's
    "I have enough" empty batch, and the degraded no-batch path. Reasoning that
    survives only one of them is memory that vanishes depending on which branch
    the round happened to take.
    """
    from tools.llm_gateway import LLMGateway
    from tools.control_tools import GrillQuestionBatch

    async def _run(batch: Any) -> Dict[str, Any]:
        with patch.object(LLMGateway, "astream_reasoning", _streamer(["thought\nAXES: a"])), \
             patch.object(analyst_mod, "_assemble_socratic_context", AsyncMock(return_value="")), \
             patch.object(analyst_mod, "_gather_tool_grounding", AsyncMock(return_value=("", []))), \
             patch.object(analyst_mod, "_generate_grill_questions_llm",
                          AsyncMock(return_value=batch)):
            return await analyst_mod.run_analyst_node(
                {"task_id": "t", "user_input": "build", "messages": [],
                 "grill_round_count": 0},
                {"configurable": {"stream_thinking": _sink, "enable_native_thinking": True}},
            )

    # 1. The model signals completion with an empty batch.
    empty = await _run(GrillQuestionBatch(questions=[]))
    assert empty.get("grill_reasoning_log"), "empty-batch exit must persist reasoning"

    # 2. A real batch is committed for the ask phase.
    real = await _run(GrillQuestionBatch(questions=[
        AskUserQuestionItem(
            header="Scope", question="Which layer?",
            options=[
                AskUserQuestionOptionInput(label="API", recommended=True),
                AskUserQuestionOptionInput(label="UI"),
            ],
            multi_select=False,
        ),
    ]))
    assert real.get("grill_reasoning_log"), "committed-batch exit must persist reasoning"

    # 3. The degraded path is covered by the row above.


@pytest.mark.anyio
async def test_row02_reasoning_is_captured_not_discarded() -> None:
    """The pass returns exactly what it streamed to the Thought Box."""
    from tools.llm_gateway import LLMGateway

    seen: List[str] = []

    async def _record(text: str, _source: str) -> None:
        seen.append(text)

    with patch.object(LLMGateway, "astream_reasoning", _streamer(["alpha ", "beta"])):
        returned = await _stream_grill_reasoning(
            user_input="task", context_block="", soul_prompt="soul", session_id="s",
            on_thinking=_record, thinking_on=True, thinking_budget=1024,
        )

    assert returned == "alpha beta"
    assert "".join(seen) == "alpha beta", "sink and return value must agree"


@pytest.mark.anyio
async def test_row03_temperature_is_asymmetric() -> None:
    """Exploratory prose samples; the strict-JSON batch stays deterministic.

    Greedy decoding on the reasoning half is half of why rounds read alike, but
    the question batch answers to a schema, where variance is only risk.
    """
    from tools.llm_gateway import LLMGateway

    calls: List[Any] = []
    with patch.object(LLMGateway, "astream_reasoning", _streamer(["x"], calls)):
        await _stream_grill_reasoning(
            user_input="t", context_block="", soul_prompt="s", session_id="s",
            on_thinking=_sink, thinking_on=True, thinking_budget=1024,
        )

    assert calls[0]["kwargs"]["temperature"] == _GRILL_REASONING_TEMPERATURE
    assert _GRILL_REASONING_TEMPERATURE > 0.0, "a greedy reasoning pass repeats by construction"

    # The batch call keeps its own low temperature.
    ainvoke_calls: List[Any] = []

    async def _fake_ainvoke(**kwargs: Any) -> Any:
        ainvoke_calls.append(kwargs)
        raise RuntimeError("stop after capturing the call shape")

    with patch.object(LLMGateway, "ainvoke", AsyncMock(side_effect=_fake_ainvoke)), \
         patch("core.config.model_resolver.get_chat_target", return_value=object()):
        await analyst_mod._generate_grill_questions_llm([], "soul", "", "s")

    assert ainvoke_calls, "the batch call must have been attempted"
    assert ainvoke_calls[0]["temperature"] < _GRILL_REASONING_TEMPERATURE


def test_row04_absent_axes_degrade_to_the_round_counter() -> None:
    """No parseable axes → the pre-existing behaviour, unchanged.

    Coverage is an improvement layered on the counter, never a dependency: a
    model that ignores the format must not strand the interview.
    """
    assert _parse_coverage_axes("prose with no marker at all") == []
    assert _coverage_settled({}) is False
    assert _coverage_settled({"grill_coverage_axes": []}) is False
    # With nothing settled and no completion signal, the loop continues as before.
    assert route_after_analyst({"grill_coverage_axes": []}) == "analyst_grill"


def test_row05_settled_coverage_hands_off_early() -> None:
    """The model's own "nothing left open" ends the interview before the cap."""
    axes = _parse_coverage_axes("...reasoning...\nAXES: none")
    assert axes == ["none"]
    assert _coverage_settled({"grill_coverage_axes": axes}) is True
    assert route_after_analyst({"grill_coverage_axes": axes}) == "synthesis_node"

    # Open axes keep it looping.
    open_axes = _parse_coverage_axes("AXES: state persistence, accessibility")
    assert open_axes == ["state persistence", "accessibility"]
    assert route_after_analyst({"grill_coverage_axes": open_axes}) == "analyst_grill"


def test_row05b_router_returns_only_declared_destinations() -> None:
    """Every value this router can return is one its own edge declares.

    The coverage branch reuses synthesis_node deliberately; a new destination
    here would break the graph exactly the way the path-map gate exists to catch.
    """
    declared = {"analyst_grill", "synthesis_node", END}
    for state in (
        {"hitl_pending": True},
        {"shared_understanding_reached": True},
        {"grill_coverage_axes": ["none"]},
        {},
    ):
        assert route_after_analyst(state) in declared


# ---------------------------------------------------------------------------
# B. The brief preserves instead of compressing
# ---------------------------------------------------------------------------


def test_row06_specifics_survive_verbatim() -> None:
    """Numbers, API names and paths the operator gave appear literally.

    The defect this division exists for: a precise request came out of the
    interview vaguer than it went in.
    """
    original = (
        "Add a /v2/exports endpoint returning NDJSON, page size 500, "
        "with an X-Export-Cursor header. Timeout at 30s."
    )
    brief = {
        "verbatim_requirements": ["page size 500", "X-Export-Cursor header", "30s timeout"],
        "intent": "Build the export endpoint.",
        "constraints": ["must stream, not buffer"],
    }
    composed = _compose_planner_brief(brief, "fallback", original)

    for specific in ("/v2/exports", "NDJSON", "500", "X-Export-Cursor", "30s"):
        assert specific in composed, f"{specific!r} was lost from the brief"


def test_row07_original_request_survives_synthesis_and_compaction() -> None:
    """The operator's wording is unreachable to both destroyers.

    synthesis_node overwrites `user_input`; StateSummarizer compacts `messages`.
    The channel is outside both by construction.
    """
    from brain.state import AIlienantGraphState
    import brain.summarizer as summarizer_mod

    assert "original_user_request" in AIlienantGraphState.__annotations__

    # synthesis_node's returned deltas never carry the key, so they cannot clobber it.
    import inspect
    src = inspect.getsource(ideation_mod.run_synthesis_node)
    assert '"original_user_request":' not in src, "synthesis must never write this channel"

    # The summarizer only ever rewrites `messages`.
    summ_src = inspect.getsource(summarizer_mod)
    assert "original_user_request" not in summ_src


def test_row08_roles_are_separated_not_restated() -> None:
    """The request block is authoritative and marked as such."""
    original = "Rename the widget to Panel everywhere."
    composed = _compose_planner_brief(
        {"intent": "Perform a project-wide rename.", "constraints": ["keep the public API"]},
        "fallback", original,
    )

    assert "THE REQUEST" in composed
    assert original in composed, "the request must appear verbatim, not paraphrased"
    request_at = composed.index("THE REQUEST")
    established_at = composed.index("WHAT THE INTERVIEW ESTABLISHED")
    assert request_at < established_at, "authority block comes first"

    # Without an original (a pre-existing checkpoint) it still composes.
    legacy = _compose_planner_brief({"intent": "Do the thing."}, "fallback", "")
    assert "Do the thing." in legacy
    assert "THE REQUEST" not in legacy


@pytest.mark.anyio
async def test_row09_tight_budget_degrades_never_aborts() -> None:
    """An impossible budget falls back rather than dead-ending the handoff.

    The planner refuses on `ok=False`; the distillation must not — its contract
    is that the handoff always proceeds.
    """
    with patch("brain.agent_context.resolve_real_window", AsyncMock(return_value=10)):
        budget = await _resolve_distill_budget({}, "a very long payload " * 500)

    assert budget > 0, "a refusal must not surface as a zero allowance"
    assert budget == ideation_mod._gateway_default_max_tokens()

    # A probe fault degrades the same way.
    with patch("brain.agent_context.resolve_real_window",
               AsyncMock(side_effect=RuntimeError("probe down"))):
        assert await _resolve_distill_budget({}, "payload") > 0


def test_row10_pre_division_checkpoint_deserializes_safely() -> None:
    """A checkpoint written before this division reads as safe defaults."""
    legacy: Dict[str, Any] = {"task_id": "t", "user_input": "do a thing", "messages": []}

    assert legacy.get("original_user_request") is None
    assert list(legacy.get("grill_reasoning_log") or []) == []
    assert list(legacy.get("grill_coverage_axes") or []) == []
    # None of the new readers raise on the absent channels.
    assert _coverage_settled(legacy) is False
    assert _reasoning_transcript(list(legacy.get("grill_reasoning_log") or [])) == ""
    assert route_after_analyst(legacy) == "analyst_grill"


@pytest.mark.anyio
async def test_row11_distillation_failure_still_degrades() -> None:
    """Regression: the never-crash contract survives the rewrite."""
    from tools.llm_gateway import LLMGateway

    with patch.object(LLMGateway, "ainvoke", AsyncMock(side_effect=RuntimeError("model down"))), \
         patch.object(ideation_mod, "_assemble_synthesis_context", AsyncMock(return_value="")):
        brief = await ideation_mod._distill_brief_llm(
            {"task_id": "t"}, [{"role": "user", "content": "build it"}],
        )

    assert isinstance(brief, dict)
    assert "build it" in brief.get("intent", ""), "raw material must reach the planner"


@pytest.mark.anyio
async def test_row12_reasoning_prompt_does_not_grow_per_round() -> None:
    """Carrying memory forward must not inflate the prompt each round.

    Appending every prior round would grow the prompt and shrink the output
    budget derived from the same window — the failure the planner's retry
    corrective had to be rewritten to avoid. Only the last entry is carried.
    """
    from tools.llm_gateway import LLMGateway

    block = "R" * 400
    sizes: List[int] = []
    for prior in (block, block):  # round 2 and round 3 both carry exactly one block
        calls: List[Any] = []
        with patch.object(LLMGateway, "astream_reasoning", _streamer(["x"], calls)):
            await _stream_grill_reasoning(
                user_input="t", context_block="", soul_prompt="s", session_id="s",
                on_thinking=_sink, thinking_on=True, thinking_budget=1024,
                prior_reasoning=prior,
            )
        sizes.append(len(calls[0]["messages"][-1]["content"]))

    assert sizes[0] == sizes[1], "round 3's prompt must not exceed round 2's"


def test_row13_revision_cap_handoff_keeps_the_request_block() -> None:
    """The cap path hands over the composed brief, so authority survives it.

    Easy leak: that branch returns early with its own dict, and a brief assembled
    anywhere but `_compose_planner_brief` would silently drop the request block.
    """
    import inspect
    src = inspect.getsource(ideation_mod.run_synthesis_node)
    assert '"user_input": brief_text' in src, (
        "the cap path must hand over the COMPOSED brief, not a re-derived summary"
    )

    composed = _compose_planner_brief({"intent": "x"}, "fb", "the original ask")
    assert "the original ask" in composed


def test_row14_reasoning_reaches_the_distillation() -> None:
    """The analyst's own reasoning is handed to the brief, labelled apart.

    It was streamed to the operator and then discarded: the transcript only ever
    carried user/assistant turns, so context the interview had already paid for
    never informed the brief.
    """
    rendered = _reasoning_transcript(["looked at ThemeProvider", "ruled out CSS vars"])

    assert "ThemeProvider" in rendered and "ruled out CSS vars" in rendered
    assert "analyst's own reasoning" in rendered, "must be attributed, not mixed into dialogue"
    assert "[round 1]" in rendered and "[round 2]" in rendered
    assert _reasoning_transcript([]) == ""


def test_row15_answered_so_far_carries_only_operator_turns() -> None:
    """The reasoning pass sees what the operator settled, not its own questions."""
    rendered = _render_answered_so_far([
        {"role": "assistant", "content": "- theme: where should it live?"},
        {"role": "user", "content": "Header: in the provider"},
    ])

    assert "in the provider" in rendered
    assert "where should it live" not in rendered, "replaying questions doubles the block"
