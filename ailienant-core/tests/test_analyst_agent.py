# tests/test_analyst_agent.py
"""Phase 4.1.5 DoD — AnalystAgent SOUL.md persona integration.

Five tests:
  A.  Hot-reload — SoulManager invalidates its cache when SOUL.md's mtime advances.
  B.  Fallback — missing file → built-in 🐜 + Socratic default prompt.
  B2. R6 — directory misconfiguration falls back gracefully (no IsADirectoryError).
  C.  R1 — Analyst node returns no phantom state keys and is strictly ReadOnly.
  D.  Foreign-import fence — planner/coder/orchestrator/researcher MUST NEVER
      import brain.personality (cognitive isolation per blueprint §3.4).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest


# ── Test A — hot-reload ──────────────────────────────────────────────────────


def test_soul_manager_hot_reloads_on_mtime_change(tmp_path: Path) -> None:
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("version one 🐜", encoding="utf-8")

    from brain.personality import SoulManager

    mgr = SoulManager(path=soul_file)

    first = mgr.get_prompt()
    assert "version one" in first

    # Force mtime advance — Windows FAT-like filesystems have 2-second mtime
    # resolution, so we sleep a touch + explicitly bump utime as belt-and-braces.
    time.sleep(0.05)
    soul_file.write_text("version two 🐜🐜", encoding="utf-8")
    new_mtime = time.time() + 1.0
    os.utime(soul_file, (new_mtime, new_mtime))

    second = mgr.get_prompt()
    assert "version two" in second

    # Third call without changes — must serve from cache (same content).
    third = mgr.get_prompt()
    assert third == second


# ── Test B — missing-file fallback ───────────────────────────────────────────


def test_soul_manager_returns_default_fallback_when_missing(tmp_path: Path) -> None:
    from brain.personality import SoulManager

    mgr = SoulManager(path=tmp_path / "does_not_exist.md")
    prompt = mgr.get_prompt()

    assert "🐜" in prompt, "fallback must contain the canonical ant emoji"
    assert "Socratic" in prompt, "fallback must identify the persona as Socratic"

    # No spurious caching of empty content — two calls return the same fallback.
    assert mgr.get_prompt() == prompt


# ── Test B2 — R6 directory-misconfiguration guard ────────────────────────────


def test_soul_manager_falls_back_when_path_is_a_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from brain.personality import SoulManager

    # tmp_path is itself a directory — simulates AILIENANT_SOUL_PATH with
    # a trailing slash, which would otherwise raise IsADirectoryError on read.
    mgr = SoulManager(path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="PERSONALITY_MANAGER"):
        prompt = mgr.get_prompt()

    assert "🐜" in prompt
    # Operator-friendly diagnostic must fire and mention "DIRECTORY".
    assert any(
        "DIRECTORY" in record.getMessage() for record in caplog.records
    ), f"Expected DIRECTORY warning, got: {[r.getMessage() for r in caplog.records]}"


# ── Grill-round harness ──────────────────────────────────────────────────────
#
# run_analyst_node now suspends each round on native interrupt(), which needs a
# live runnable context. These seal both boundaries: the structured
# question-batch LLM call, and the clarification suspend (via the same injected
# `analyst_clarification_fn` seam pattern the agentic cell uses), so the node's
# own logic is testable without a graph run or a live model.

_SOUL_SENTINEL = "SOUL_SENTINEL_NEVER_LEAK_THIS_STRING"


def _batch_of_one() -> Any:
    from tools.control_tools import (
        AskUserQuestionItem,
        AskUserQuestionOptionInput,
        GrillQuestionBatch,
    )

    return GrillQuestionBatch(questions=[
        AskUserQuestionItem(
            header="Scope",
            question="Which surface should the endpoint live on?",
            options=[
                AskUserQuestionOptionInput(label="REST", recommended=True),
                AskUserQuestionOptionInput(label="WebSocket"),
            ],
        ),
    ])


def _empty_batch() -> Any:
    from tools.control_tools import GrillQuestionBatch

    return GrillQuestionBatch(questions=[])


def _answer_first_option() -> Any:
    """A clarification seam that answers every question with its first option."""

    async def _fn(question_dicts: List[Dict[str, Any]]) -> Dict[str, Any]:
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

    return _fn


async def _run_grill_round(
    state: Dict[str, Any], batch: Any, clarification_fn: Any
) -> Dict[str, Any]:
    """Drives one full round through BOTH graph super-steps: the generate
    phase (commits `pending_grill_batch` to state and returns, no interrupt)
    then, when a batch was actually asked, the ask phase (reads it back and
    resolves via the seam, as its first action) — mirrors exactly how
    brain/ideation.py's analyst_grill self-loop drives the two invocations.

    Returns the two deltas merged (`messages` concatenated, matching the
    `_merge_messages` reducer's append semantics) so existing single-call
    assertions still read the full round's outcome in one place.
    """
    async def _fake_generate(*_a: Any, **_k: Any) -> Any:
        return batch

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=_SOUL_SENTINEL
    ), patch(
        "agents.analyst._generate_grill_questions_llm", new=_fake_generate
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ):
        from agents.analyst import run_analyst_node

        generate_delta = await run_analyst_node(state, {})
        if not generate_delta.get("pending_grill_batch"):
            return generate_delta

        ask_state = {**state, **generate_delta}
        ask_delta = await run_analyst_node(
            ask_state, {"configurable": {"analyst_clarification_fn": clarification_fn}}
        )
        return {
            **generate_delta,
            **ask_delta,
            "messages": generate_delta.get("messages", []) + ask_delta.get("messages", []),
        }


# ── 13.1.9 — grounding-skip visibility ───────────────────────────────────────


@pytest.mark.anyio
async def test_gather_tool_grounding_emits_a_marker_when_skipped_for_no_workspace() -> None:
    """A silent no-op here previously left "the analyst ran no tools" with no
    trace at all, indistinguishable from a grounding pass that found nothing.
    """
    from agents.analyst import _gather_tool_grounding
    from langchain_core.runnables import RunnableConfig

    push_activity = AsyncMock()
    config: RunnableConfig = {"configurable": {"push_activity": push_activity}}

    block, trace = await _gather_tool_grounding({}, config, "task-1")

    assert block == ""
    assert trace == []
    push_activity.assert_awaited_once_with("retrieval", metric="no workspace to ground against")


@pytest.mark.anyio
async def test_gather_tool_grounding_skip_is_a_noop_with_no_push_activity_configured() -> None:
    from agents.analyst import _gather_tool_grounding

    block, trace = await _gather_tool_grounding({}, {}, "task-1")
    assert block == ""
    assert trace == []


# ── Test C — R1 state-key contract + ReadOnly policy ─────────────────────────


@pytest.mark.anyio
async def test_analyst_agent_read_only_policy_and_no_phantom_state_keys() -> None:
    """run_analyst_node must not mutate vfs_buffer / pending_patches / generated_code,
    must not leak the soul_prompt sentinel into messages, and every returned key
    must be a declared AIlienantGraphState field."""
    sentinel = _SOUL_SENTINEL

    state: Dict[str, Any] = {
        "task_id": "analyst-test",
        "user_input": "I want to add a new endpoint.",
        "messages": [],
        "errors": [],
        "security_flags": [],
        "hitl_pending": False,
        "shared_understanding_reached": False,
    }

    result = await _run_grill_round(state, _batch_of_one(), _answer_first_option())

    # ReadOnly policy: writer-channel keys must NOT appear in the result dict.
    assert "vfs_buffer" not in result
    assert "pending_patches" not in result
    assert "generated_code" not in result

    # R1 — every returned key must be a declared AIlienantGraphState field.
    allowed_state_keys = {
        "messages",
        "hitl_pending",
        "shared_understanding_reached",
        "errors",
        "security_flags",
        "grill_round_count",
        "pending_grill_batch",
    }
    extras = set(result.keys()) - allowed_state_keys
    assert not extras, f"Analyst leaked non-state keys: {extras}"

    # Soul prompt sentinel must NOT appear in any returned message content.
    surfaced = "\n".join(m.get("content", "") for m in result.get("messages", []))
    assert sentinel not in surfaced, (
        "soul_prompt is ephemeral — it must NEVER reach state.messages."
    )


# ── Test C1b — grill narration ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_generate_phase_narrates_grounding_and_composing() -> None:
    """Without this, the whole interview phase narrated nothing on the
    Glass-Box Timeline — the generic upstream "Understanding your request"
    marker was the only thing on screen until the first card appeared,
    regardless of how many rounds ran in between."""
    narrated: List[str] = []

    async def _narrate(label: str) -> None:
        narrated.append(label)

    async def _fake_generate(*_a: Any, **_k: Any) -> Any:
        return _batch_of_one()

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=_SOUL_SENTINEL
    ), patch(
        "agents.analyst._generate_grill_questions_llm", new=_fake_generate
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ):
        from agents.analyst import run_analyst_node

        await run_analyst_node(state, {"configurable": {"narrate": _narrate}})

    assert narrated == ["grill_grounding", "grill_composing_questions"]


# ── Test C1c — grill reasoning reaches the Thought Box ───────────────────────


@pytest.mark.anyio
async def test_generate_phase_streams_reasoning_without_touching_the_json_contract() -> None:
    """The interview is the one phase whose model work was entirely invisible.

    Two things must hold together: the reasoning reaches the sink, AND the
    question batch is still drafted under its strict json_object contract — a
    reasoning preamble folded into that call would corrupt the parsed output,
    which is why the pass is a separate free-form completion.
    """
    streamed: List[str] = []
    batch_kwargs: Dict[str, Any] = {}

    async def _on_thinking(text: str, _source: str) -> None:
        streamed.append(text)

    class _Delta:
        def __init__(self, text: str) -> None:
            self.text = text
            self.source = "simulated"

    async def _fake_astream_reasoning(*_a: Any, **_k: Any) -> Any:
        for chunk in ("The endpoint's ", "auth story is unstated."):
            yield _Delta(chunk)

    async def _spy_generate(*_a: Any, **k: Any) -> Any:
        batch_kwargs.update(k)
        return _batch_of_one()

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=_SOUL_SENTINEL
    ), patch(
        "agents.analyst._generate_grill_questions_llm", new=_spy_generate
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="ctx")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ), patch(
        "tools.llm_gateway.LLMGateway.astream_reasoning", new=_fake_astream_reasoning
    ):
        from agents.analyst import run_analyst_node

        await run_analyst_node(state, {"configurable": {
            "stream_thinking": _on_thinking,
            "enable_native_thinking": True,
        }})

    assert "".join(streamed) == "The endpoint's auth story is unstated."
    assert "response_format" not in batch_kwargs, (
        "the reasoning pass must not have altered the batch call's contract"
    )


@pytest.mark.anyio
async def test_grill_reasoning_is_silent_when_the_seam_is_absent() -> None:
    """No sink wired (a benchmark run, or thinking switched off) must degrade to
    the previous behaviour, never to a crash — reasoning is best-effort."""

    async def _fake_generate(*_a: Any, **_k: Any) -> Any:
        return _batch_of_one()

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=_SOUL_SENTINEL
    ), patch(
        "agents.analyst._generate_grill_questions_llm", new=_fake_generate
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(state, {"configurable": {}})

    assert result.get("pending_grill_batch"), "the interview still produced its batch"


def test_grill_reasoning_and_draft_resolve_to_the_same_model() -> None:
    """The reasoning shown to the user must come from the model that then writes
    the questions. The draft invokes an alias, the probe and the reasoning pass
    take a bare tier — three call sites that must not be able to drift apart."""
    from agents.analyst import _GRILL_MODEL, _GRILL_TIER

    assert _GRILL_MODEL == f"ailienant/{_GRILL_TIER}", (
        "the grill's alias and tier have diverged — reasoning and questions would "
        "be produced by different models"
    )


# ── Test C2 — batched grill rounds ───────────────────────────────────────────


@pytest.mark.anyio
async def test_grill_round_asks_batch_and_folds_answers_into_messages() -> None:
    """A non-empty batch suspends for answers, then folds them back into
    `messages` id-correlated to each question's header, and stays un-finished
    so ideation's self-loop drives another round."""
    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    result = await _run_grill_round(state, _batch_of_one(), _answer_first_option())

    assert result["shared_understanding_reached"] is False
    assert result["grill_round_count"] == 1
    contents = [m["content"] for m in result["messages"]]
    # The question batch is recorded, and the picked option is folded back.
    assert any("Which surface should the endpoint live on?" in c for c in contents)
    assert any("Scope: REST" in c for c in contents)


@pytest.mark.anyio
async def test_generate_phase_commits_batch_without_suspending() -> None:
    """The generate phase alone (no pending_grill_batch in state yet) must
    commit the batch to state and return WITHOUT ever calling the
    clarification seam — interrupt() only happens in the ask phase, on the
    NEXT self-loop visit. This is the replay-safety split itself: generating
    and interrupting in the same invocation is what let a resumed round
    silently regenerate a different batch than the one the operator answered."""
    called = False

    async def _never(_q: List[Dict[str, Any]]) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {}

    async def _fake_generate(*_a: Any, **_k: Any) -> Any:
        return _batch_of_one()

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=_SOUL_SENTINEL
    ), patch(
        "agents.analyst._generate_grill_questions_llm", new=_fake_generate
    ), patch(
        "agents.analyst._assemble_socratic_context", new=AsyncMock(return_value="")
    ), patch(
        "agents.analyst._gather_tool_grounding", new=AsyncMock(return_value=("", []))
    ):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(
            state, {"configurable": {"analyst_clarification_fn": _never}}
        )

    assert called is False, "generate phase must never resolve answers itself"
    assert result["pending_grill_batch"], "the batch must be committed to state"
    assert result["pending_grill_batch"][0]["header"] == "Scope"
    # No question/answer text yet — that only appears once the ask phase folds
    # the resumed answer in.
    assert result.get("messages", []) == []


@pytest.mark.anyio
async def test_ask_phase_never_regenerates_the_batch() -> None:
    """The ask phase (pending_grill_batch already set) must resolve answers
    against the STATE-SOURCED batch and must NEVER call
    _generate_grill_questions_llm — regenerating here is exactly the
    non-determinism that misaligned answer ids on a resume replay."""
    from tools.control_tools import questions_to_pending_dicts

    question_dicts = questions_to_pending_dicts(_batch_of_one().questions)

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not regenerate the batch in the ask phase")

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
        "pending_grill_batch": question_dicts,
    }

    with patch("agents.analyst._generate_grill_questions_llm", new=_boom):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(
            state, {"configurable": {"analyst_clarification_fn": _answer_first_option()}}
        )

    assert result["pending_grill_batch"] is None, "the channel must be cleared"
    contents = [m["content"] for m in result["messages"]]
    assert any("Which surface should the endpoint live on?" in c for c in contents)
    assert any("Scope: REST" in c for c in contents)


@pytest.mark.anyio
async def test_empty_batch_signals_shared_understanding_without_suspending() -> None:
    """An empty batch is the model's completion signal — it must hand off
    without ever calling the clarification seam."""
    called = False

    async def _never(_q: List[Dict[str, Any]]) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {}

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [], "hitl_pending": False, "shared_understanding_reached": False,
    }

    result = await _run_grill_round(state, _empty_batch(), _never)

    assert result["shared_understanding_reached"] is True
    assert result["hitl_pending"] is False
    assert called is False, "an empty batch must never suspend for answers"


@pytest.mark.anyio
async def test_round_cap_forces_handoff() -> None:
    """The circuit breaker hands off once _GRILL_MAX_ROUNDS rounds have run, so
    a model that never returns an empty batch cannot interview forever."""
    from agents.analyst import _GRILL_MAX_ROUNDS

    called = False

    async def _never(_q: List[Dict[str, Any]]) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {}

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Add an endpoint.",
        "messages": [{"role": "assistant", "content": "prior"}],
        "grill_round_count": _GRILL_MAX_ROUNDS,
        "hitl_pending": False, "shared_understanding_reached": False,
    }

    result = await _run_grill_round(state, _batch_of_one(), _never)

    assert result["shared_understanding_reached"] is True
    assert called is False, "the cap must short-circuit before asking again"


@pytest.mark.anyio
async def test_free_text_agreement_still_short_circuits_on_a_fresh_turn() -> None:
    """The legacy agreement-phrase fast path stays intact for a genuinely new
    top-level turn (round_count == 0) answering a prior question."""
    called = False

    async def _never(_q: List[Dict[str, Any]]) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {}

    state: Dict[str, Any] = {
        "task_id": "analyst-test", "user_input": "Looks good, proceed.",
        "messages": [{"role": "assistant", "content": "an earlier question"}],
        "hitl_pending": False, "shared_understanding_reached": False,
    }

    result = await _run_grill_round(state, _batch_of_one(), _never)

    assert result["shared_understanding_reached"] is True
    assert called is False


# ── Test D — foreign-import fence (cognitive isolation) ──────────────────────


def test_soul_manager_not_imported_by_logic_agents() -> None:
    """Planner, Coder, Orchestrator, Researcher MUST NEVER import brain.personality.

    Static source audit — catches an accidental future import that would breach
    the cognitive-isolation fence (blueprint §3.4).
    """
    project_root = Path(__file__).resolve().parent.parent  # ailienant-core/
    logic_agents: List[Path] = [
        project_root / "agents" / "planner.py",
        project_root / "agents" / "coder.py",
        project_root / "agents" / "orchestrator.py",
        project_root / "agents" / "researcher.py",
        project_root / "agents" / "error_correction.py",
    ]
    forbidden_patterns = ("from brain.personality", "import brain.personality")

    breaches: List[str] = []
    for agent_file in logic_agents:
        assert agent_file.is_file(), f"expected logic agent missing: {agent_file}"
        source = agent_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in source:
                breaches.append(f"{agent_file.name}: contains '{pattern}'")

    assert not breaches, (
        "Cognitive-isolation fence breached — logic agents must not import "
        f"brain.personality. Breaches: {breaches}"
    )
