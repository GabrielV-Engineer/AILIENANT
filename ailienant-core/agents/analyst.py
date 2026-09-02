# ailienant-core/agents/analyst.py
#
# — Socratic "Grill Me" AnalystAgent.
#
# Implements the "Grill Me" pattern:
#   - Each round asks a BATCH of structured questions (2-4 concrete options each,
#     one recommended) in one pass, via the same request_graph_clarification /
#     ClarificationGrillCard channel ask_user_question uses — not free-text prose.
#   - Reads codebase via read_file tool before asking (avoid asking what can be known)
#   - Suspends per-round via native interrupt()/resume; a self-loop edge on
#     analyst_grill (brain/ideation.py) drives another round when needed.
#   - The LLM signals completion itself by returning an empty questions batch,
#     replacing the old free-text agreement-phrase detection as the primary path.

import asyncio
import json
import logging
import os as _os
import re as _re
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from tools.control_tools import (
    AskUserQuestionItem,
    AskUserQuestionOptionInput,
    GrillQuestionBatch,
    questions_to_pending_dicts,
)

# SOUL.md persona reader. Analyst is the EXCLUSIVE consumer of
# brain.personality cognitive-isolation fence. Other agents
# (planner, coder, orchestrator, researcher) MUST NEVER import this module —
# Test D audits the four logic-agent files for foreign imports on every CI run.
from brain.personality import soul_manager
from core.activity_context import bind_model_tier
from core.config.model_resolver import tier_for_alias
from shared.config import MODEL_MEDIUM

logger = logging.getLogger("ANALYST_AGENT")

# The live LLM path is the default; the synthetic [DEBUG Q1/Q2] script is the
# escape hatch retained for deterministic CI/UI smoke tests. Mirrors planner.py:
# set AILIENANT_ANALYST_DEBUG=1 to force the stub.
DEBUG_MODE: bool = _os.getenv("AILIENANT_ANALYST_DEBUG", "0") != "0"

# The model the whole grill runs on. The alias is the source: the question draft
# invokes it directly, while the target probe and the pre-draft reasoning pass
# take the bare tier derived from it. Keeping one source is what makes an
# operator's AILIENANT_MODEL_MEDIUM override move all three together — split, the
# user would read reasoning from one model and get questions written by another.
_GRILL_MODEL: str = MODEL_MEDIUM
_GRILL_TIER: str = tier_for_alias(_GRILL_MODEL, default="medium")

# Token ceiling for the pre-draft reasoning pass. Small on purpose, mirroring the
# planner's own ceiling: this is the conceptual narrative shown while the user
# waits, not the question batch, which carries its own budget.
_GRILL_REASONING_MAX_TOKENS: int = 512

# The reasoning pass explores; the question batch commits. Greedy decoding on the
# exploratory half made every round restate the previous one almost verbatim,
# since the round's inputs barely move. The batch call keeps its own low
# temperature — it answers to a strict JSON schema, where variance is only risk.
_GRILL_REASONING_TEMPERATURE: float = 0.6

# Marker the reasoning pass closes with, naming the dimensions IT judged relevant
# to THIS task. Parsed back out for the coverage stop; the model picks the axes,
# so nothing here presumes what a task is made of.
_AXES_MARKER: str = "AXES:"

# Cap on axes carried forward. A model that answers with a paragraph instead of a
# short list would otherwise turn the stop criterion into a prompt of its own.
_MAX_COVERAGE_AXES: int = 8

# Longest an axis label may be before it is treated as prose that missed the
# format. Generous enough for "state management across the wizard", short enough
# to reject a sentence.
_MAX_AXIS_CHARS: int = 60

_AGREEMENT_SIGNALS = frozenset([
    # English
    "looks good", "sounds good", "yes", "approved", "agreed",
    "let's go", "proceed", "i'm happy", "perfect", "solid",
    "ship it", "lgtm", "ok", "okay",
    # Spanish (user may respond in Spanish)
    "dale", "fuego", "proceder", "adelante", "de acuerdo",
    "perfecto", "bien", "listo", "lo apruebo", "seguimos",
])

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: Set[asyncio.Task[Any]] = set()

# Upper bound on reason→call→observe cycles the analyst spends gathering read-only
# diagnostics before it must commit to its next question. Bounded so a chatty model
# cannot stall the Socratic turn.
_ANALYST_TOOL_MAX_ITERS: int = 3

# Circuit breaker: an internal graph self-loop (brain/ideation.py's
# analyst_grill → analyst_grill edge) drives another round whenever the model
# hasn't yet signalled it has enough shared understanding — bounded so a model
# that never returns an empty batch can't interview forever.
_GRILL_MAX_ROUNDS: int = 3

# The "Grill Me" contract handed to the model on top of the SOUL persona. It
# instructs the model to emit a structured JSON batch of every question it
# currently needs answered (not one at a time) — the actual card rendering
# (tabs, options, "Recommended" badge, free-text "Other") is built from this
# by ClarificationGrillCard.tsx; the model never sees UI concerns, only the
# question/options data contract.
_GRILL_DIRECTIVE: str = (
    "You are running a Socratic 'Grill Me' planning session. Your job is to "
    "extract a precise, buildable plan from the user by asking every question "
    "you currently need answered, batched together so the user answers them "
    "all in one pass instead of a long back-and-forth.\n"
    "RULES:\n"
    "- Respond with ONLY a JSON object of the form "
    '{"questions": [{"header": "<=3 word tab label>", "question": "<the full '
    'question>", "context": "<optional background, or null>", "options": '
    '[{"label": "<short answer>", "description": "<optional one-sentence '
    'rationale, or null>", "recommended": <true on exactly one option>}, '
    '...2 to 4 options...], "multi_select": <true or false>}, ...]}\n'
    "- Ask 2 to 6 concrete questions per batch — everything you currently need "
    "to know, not just one. Never ask something already answered in the "
    "conversation or the workspace context below, and never repeat a previous "
    "question.\n"
    "- Every question needs 2 to 4 concrete, mutually exclusive options with "
    "exactly one marked recommended=true — never leave the user with only an "
    "open-ended blank to fill in.\n"
    "- Once you have enough shared understanding to proceed to planning, "
    'respond with {"questions": []} — an empty list, nothing else.\n'
    "- Build directly on what the user just said and on the workspace context "
    "below — reference their actual words, files, and code.\n"
    "- No prose, no markdown fences, no preamble. The JSON object is the "
    "entire response."
)


def _has_prior_socratic_exchange(messages: List[Dict[str, Any]]) -> bool:
    """Return True if the analyst has already asked at least one question."""
    return any(m.get("role") == "assistant" for m in messages)


# Trimmed from both ends of the message AND of each clause below — covers the
# punctuation a real reply carries ("yes.", "dale!", "¿bien?") without
# inventing a tokenizer for what is otherwise a set-membership check.
_AGREEMENT_STRIP_CHARS = " \t\r\n.,;:!¡¿?\"'"
_CLAUSE_SPLIT_RE = _re.compile(r"[,.;]+")

# A small, fixed set of leading connectives that don't change a clause's
# meaning ("let's proceed" IS "proceed") — stripped before the signal-set
# lookup so a clause built from one of these plus a real signal still
# matches, without loosening the lookup itself into a substring search.
_AGREEMENT_LEADING_FILLERS = ("let's ", "lets ", "let us ", "we can ", "we'll ", "please ")


def _is_agreement_clause(clause: str) -> bool:
    """A clause counts as agreement if it IS a signal, or a signal once a
    leading filler connective ("let's ", "please ", …) is removed."""
    if clause in _AGREEMENT_SIGNALS:
        return True
    for filler in _AGREEMENT_LEADING_FILLERS:
        if clause.startswith(filler):
            return clause[len(filler):] in _AGREEMENT_SIGNALS
    return False


def _is_agreement(user_input: str) -> bool:
    """Detect a bare agreement reply — anchored to the message's clauses, not
    an unanchored substring search.

    A short affirmation ("yes", "looks good", "dale") ends the grill; a
    substantive, still-elaborating answer that merely OPENS with one of those
    words ("Yes, establish component files for Header, HeroSlider...") must
    not, since `signal in text` previously treated the two identically
    (DEBT-180). A message counts as agreement only if it is a single
    agreement clause, or every comma/period-separated clause is
    independently one — the latter is load-bearing: the frontend's own
    canonical hand-off phrase (`AGREEMENT_SIGNAL = 'Looks good, proceed.'` in
    Workspace.tsx) is two signals joined by a comma, and "looks good, let's
    proceed" (a pre-existing regression-tested phrase) combines a signal with
    a filler-plus-signal clause, neither literally one entry in
    `_AGREEMENT_SIGNALS`. A clause that ISN'T itself an agreement clause
    disqualifies the whole message — the safe direction for this fast path:
    a missed match just asks one more round, while a false match prematurely
    ends a still-elaborating answer.
    """
    text = user_input.lower().strip(_AGREEMENT_STRIP_CHARS)
    if not text:
        return False
    if _is_agreement_clause(text):
        return True
    clauses = [c.strip(_AGREEMENT_STRIP_CHARS) for c in _CLAUSE_SPLIT_RE.split(text)]
    clauses = [c for c in clauses if c]
    return bool(clauses) and all(_is_agreement_clause(c) for c in clauses)


_INTENT_SYSTEM_PROMPT: str = (
    "You are an AnalystAgent performing Pre-Dream Reflection. "
    "Given the last 3–5 user messages, produce ONE sentence (≤30 words) "
    "summarising the user's primary coding intent. "
    "Respond with only that sentence — no preamble, no punctuation beyond the sentence."
)


async def generate_intent_summary_llm(user_messages: List[str], task_id: str = "") -> str:
    """ One-shot LLM call to summarise last N user intents (Pre-Dream Reflection)."""
    from tools.llm_gateway import LLMGateway   # deferred — avoids circular import
    from shared.config import MINI_JUDGE_MODEL  # reuse the fast mini-judge model
    combined = "\n".join(f"- {m}" for m in user_messages)
    result = await LLMGateway.ainvoke(
        messages=[
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {"role": "user",   "content": combined},
        ],
        model=MINI_JUDGE_MODEL,
        temperature=0.0,
        max_tokens=60,
        session_id=task_id,
    )
    return str(result).strip()


def _render_batch_for_history(question_dicts: List[Dict[str, Any]]) -> str:
    """Flatten a question batch into a readable transcript line for `messages`
    history (and for the distillation prompt downstream) — the actual UI comes
    from ClarificationGrillCard, not this string.

    Takes the state-sourced ``pending_grill_batch`` dicts (never the live
    Pydantic batch fresh out of the LLM) so the ask phase always renders
    exactly what was asked — the same reason it resolves answers against
    those dicts rather than a replay-regenerated batch.
    """
    return "\n".join(f"- {q['header']}: {q['question']}" for q in question_dicts)


def _fold_answers_into_summary(
    question_dicts: List[Dict[str, Any]], resolved: Dict[str, Any]
) -> str:
    """Turn a resumed clarification's `answers` list into one readable line per
    question, id-correlated back to its header (mirrors
    brain/agentic_cell.py::_resolve_pending_clarification's fold-in pattern)."""
    by_id = {q["id"]: q for q in question_dicts}
    answers_list = resolved.get("answers") or []
    lines = [
        f"{by_id.get(a.get('id'), {}).get('header', a.get('id'))}: "
        f"{', '.join(a.get('selected_labels') or []) or a.get('free_text') or '(no answer)'}"
        for a in answers_list
    ]
    return "; ".join(lines) or "(the operator gave no answer)"


async def _resolve_grill_answers(
    task_id: str,
    question_dicts: List[Dict[str, Any]],
    config: Optional[RunnableConfig],
) -> Dict[str, Any]:
    """Suspend this round for the human's answers to `question_dicts`.

    Mirrors brain/agentic_cell.py::_resolve_pending_clarification's contract: an
    injected ``analyst_clarification_fn`` seam so the node stays unit-testable
    outside a live graph run (native ``interrupt()`` requires a runnable
    context), else the real native-``interrupt()`` suspend so the runtime is
    freed until the operator replies.
    """
    clarification_fn = (config or {}).get("configurable", {}).get("analyst_clarification_fn")
    if clarification_fn is not None:
        raw = await clarification_fn(question_dicts)
        return dict(raw or {})
    from core.hitl import request_graph_clarification  # deferred — avoids import cycle

    return request_graph_clarification(session_id=task_id, questions=question_dicts)


def _debug_grill_batch(round_count: int) -> "GrillQuestionBatch":
    """Deterministic synthetic batch for the CI/UI smoke-test escape hatch —
    2 questions on the first round, then an empty batch (done) on any round
    after, exercising both the ask and the completion path deterministically."""
    if round_count > 0:
        return GrillQuestionBatch(questions=[])
    return GrillQuestionBatch(questions=[
        AskUserQuestionItem(
            header="Deliverable",
            question=(
                "[DEBUG Q1] What is the primary deliverable, and what does "
                "'done' look like?"
            ),
            options=[
                AskUserQuestionOptionInput(
                    label="A working feature with all existing tests green "
                    "plus new unit tests covering the changed behaviour.",
                    recommended=True,
                ),
                AskUserQuestionOptionInput(
                    label="A minimal proof-of-concept; tests deferred.",
                ),
            ],
        ),
        AskUserQuestionItem(
            header="Constraints",
            question=(
                "[DEBUG Q2] What are the non-functional constraints "
                "(performance budget, security surface, dependency restrictions)?"
            ),
            options=[
                AskUserQuestionOptionInput(
                    label="O(n) complexity max, no new external deps, all "
                    "inputs sanitised at the boundary.",
                    recommended=True,
                ),
                AskUserQuestionOptionInput(
                    label="No hard constraints — optimize for speed of delivery.",
                ),
            ],
        ),
    ])


async def run_analyst_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """LangGraph node: Socratic Grill Me AnalystAgent.

    Each round is TWO graph super-steps (brain/ideation.py's analyst_grill self-loop
    drives both), not one — this split is load-bearing, not cosmetic:

      1. **Generate phase** (`pending_grill_batch` empty): composes a batch of every
         question the model currently needs answered (2-6, each with concrete
         options), commits it to `pending_grill_batch`, and returns — ending this
         super-step with NO interrupt() call.
      2. **Ask phase** (`pending_grill_batch` set, next self-loop visit):
         `_resolve_grill_answers` on the STATE-SOURCED batch is the node's first
         action, suspending via native interrupt()/resume on the same clarification
         channel ask_user_question uses (ClarificationGrillCard renders it). Folds
         the resumed answers into `messages` and clears the channel.

    Why: LangGraph replays a node from the top on every resume. Generating the
    batch and calling interrupt() in the SAME invocation (the original single-phase
    design) meant every resume re-ran the LLM call that composed the questions —
    non-deterministically regenerating a batch the operator never saw, silently
    misaligning the positional `q{i}` ids `_fold_answers_into_summary` correlates
    answers against, and in the worst case producing an EMPTY replay batch that
    skips interrupt() entirely and discards the resume value outright. This is the
    exact hazard brain/agentic_cell.py's `pending_exec_command`/`pending_tool_call`/
    `pending_hitl_request` defer-then-interrupt-first pattern exists to avoid;
    `pending_grill_batch` applies the same fix here.

    The model signals completion itself by returning an EMPTY questions batch in
    the generate phase — replacing free-text agreement-phrase detection as the
    primary completion path. `grill_round_count` bounds the loop (_GRILL_MAX_ROUNDS)
    since a local Python counter cannot survive across super-steps.

    The live path grounds each round in the workspace (active file + GraphRAG, via
    the same assembler the Natt pane uses) and mirrors the user's language.
    """
    task_id: str = state.get("task_id", "")
    messages: List[Dict[str, Any]] = list(state.get("messages", []))
    round_count: int = int(state.get("grill_round_count", 0) or 0)
    pending_batch: Optional[List[Dict[str, Any]]] = state.get("pending_grill_batch")

    # Glass-Box Timeline narration (mirrors brain/ideation.py::run_synthesis_node's
    # _emit helper). Without this the whole interview phase was silent — the
    # generic "Understanding your request" marker from an earlier upstream node
    # was the only thing on screen until the first question card appeared,
    # regardless of how many grounding/generation rounds ran in between.
    _narrate = (config or {}).get("configurable", {}).get("narrate")
    # Reasoning sink + native-thinking prefs — the same off-state seam the planner
    # and coder read (agents/planner.py). Without these the interview was the one
    # phase that showed the user nothing at all while it worked.
    _on_thinking = (config or {}).get("configurable", {}).get("stream_thinking")
    _thinking_on = bool((config or {}).get("configurable", {}).get("enable_native_thinking"))
    _thinking_budget = int(
        (config or {}).get("configurable", {}).get("thinking_budget_tokens") or 4096
    )

    async def _emit(node_name: str) -> None:
        if _narrate is not None:
            await _narrate(node_name)

    # ── Ask phase ─────────────────────────────────────────────────────────
    if pending_batch:
        resolved = await _resolve_grill_answers(task_id, pending_batch, config)
        answer_summary = _fold_answers_into_summary(pending_batch, resolved)

        # Companion decision point: this round closed. Consumes ONLY this
        # round's own batch + answers (both already round-local, never the
        # accumulated tool_dispatch_trace) — never blocks the graph.
        from brain.coder_companion import schedule_agent_companion, build_ideation_companion_request

        _answers_by_id = {
            a.get("id"): (
                ", ".join(a.get("selected_labels") or []) or a.get("free_text") or ""
            )
            for a in (resolved.get("answers") or [])
        }
        schedule_agent_companion(
            state, "ideation", round_count,
            lambda: build_ideation_companion_request(
                session_id=task_id, task_id=task_id, attempt_ordinal=round_count,
                task_description="Clarifying requirements with the operator",
                question_batch=pending_batch, resolved_answers=_answers_by_id,
            ),
        )

        return {
            "hitl_pending": False,
            "shared_understanding_reached": False,
            "messages": [
                {"role": "assistant", "content": _render_batch_for_history(pending_batch)},
                {"role": "user", "content": answer_summary},
            ],
            "pending_grill_batch": None,
        }

    # ── Generate phase ───────────────────────────────────────────────────
    user_input: str = state.get("user_input", "")
    has_prior = _has_prior_socratic_exchange(messages)

    # Only meaningful on a genuinely fresh top-level invocation (round_count
    # == 0): on a self-loop continuation, state["user_input"] is stale — the
    # graph never re-reads a new chat turn between internal rounds, since each
    # round's answer is resolved via interrupt()/resume instead — so checking
    # it there would false-positive on original task text that happens to
    # contain an agreement word (e.g. "Yes, add a dark mode toggle").
    if round_count == 0 and has_prior and _is_agreement(user_input):
        logger.info("AnalystAgent: agreement detected — shared understanding reached.")
        new_messages: List[Dict[str, Any]] = (
            [{"role": "user", "content": user_input}] if user_input else []
        )
        return {
            "shared_understanding_reached": True,
            "hitl_pending": False,
            "messages": new_messages,
        }

    # Accumulate the human's answer from the previous TOP-LEVEL turn (if any).
    # Guard: only on the true first round — on the first turn, user_input is
    # the original task brief, not a Socratic response; don't pollute history.
    new_messages = (
        [{"role": "user", "content": user_input}]
        if round_count == 0 and has_prior and user_input
        else []
    )

    # fetch the persona prompt as an EPHEMERAL local variable.
    # CRITICAL: soul_prompt is NEVER written to state.messages or returned in the
    # result dict (R1 — state-key contract). LLM call will receive
    # it as the system message body; for now it is held locally and only its
    # length + emoji flag are logged, so tests can audit integration without
    # leaking prompt content.
    soul_prompt: str = soul_manager.get_prompt()
    logger.info(
        "AnalystAgent: SOUL prompt loaded (%d chars, contains_emoji=%s).",
        len(soul_prompt),
        "🐜" in soul_prompt,
    )

    if round_count >= _GRILL_MAX_ROUNDS:
        logger.warning(
            "AnalystAgent: grill round cap (%d) reached — forcing handoff.",
            _GRILL_MAX_ROUNDS,
        )
        return {
            "shared_understanding_reached": True,
            "hitl_pending": False,
            "messages": new_messages,
        }

    dispatch_trace: List[Dict[str, Any]] = []
    batch: Optional[GrillQuestionBatch]
    # Filled by the generator below with why it gave up, so a failure is reported
    # as what it actually was. Stays empty on the DEBUG path, which cannot fail.
    _grill_failure: Dict[str, str] = {}
    round_reasoning: str = ""

    def _with_reasoning(result: Dict[str, Any]) -> Dict[str, Any]:
        """Attach this round's captured reasoning and the axes it named.

        Applied to every exit that advances the round, so a round whose reasoning
        ran is never lost to whichever branch the batch outcome takes. Both keys
        are omitted when there is nothing to record, keeping the no-op turn's
        minimal state-delta contract (the same reason tool_dispatch_trace is
        conditional).
        """
        if round_reasoning:
            result["grill_reasoning_log"] = [round_reasoning]
            axes = _parse_coverage_axes(round_reasoning)
            if axes:
                result["grill_coverage_axes"] = axes
        return result
    if DEBUG_MODE:
        logger.info("AnalystAgent (DEBUG): synthetic question batch generated.")
        batch = _debug_grill_batch(round_count)
    else:
        # Before committing to a batch, ground it in the workspace + optionally
        # call READ_ONLY diagnostic tools. Best-effort and bounded; any failure
        # degrades to the context-only batch — the analyst must never crash
        # the graph. Re-runs once on this round's own interrupt()/resume
        # replay (accepted, bounded cost — matches the pre-existing per-round
        # grounding cost this node already had before this redesign).
        # Bind-and-forget — the enclosing node wrapper's `finally` owns cleanup
        # (`ideation.py::_guarded` for this subgraph, mirroring `_instrument_node`;
        # see `core/activity_context.py`'s docstring). The grill is pinned to
        # one tier for its whole lifetime (module docstring above _GRILL_TIER),
        # so binding once here covers grounding, reasoning and question rows alike.
        bind_model_tier(_GRILL_TIER)
        await _emit("grill_grounding")
        context_block = await _assemble_socratic_context(state)
        grounding, dispatch_trace = await _gather_tool_grounding(state, config, task_id)
        if grounding:
            context_block = (
                f"{context_block}\n\n{grounding}" if context_block else grounding
            )
        # The reasoning pass gets what the previous round concluded and what the
        # operator has since answered. Without them it ran on inputs that barely
        # move between rounds — the same task text, the same workspace — and
        # returned the same reasoning each time. Only the LAST entry is carried:
        # see the channel's own note in brain/state.py on why this must not
        # accumulate into the prompt.
        _prior_log: List[str] = list(state.get("grill_reasoning_log") or [])
        # Prefer the request as the operator actually wrote it; user_input is stale
        # on a self-loop continuation (see the round_count == 0 guard above).
        _task_text = state.get("original_user_request") or user_input
        round_reasoning = await _stream_grill_reasoning(
            user_input=_task_text,
            context_block=context_block,
            soul_prompt=soul_prompt,
            session_id=task_id,
            on_thinking=_on_thinking,
            thinking_on=_thinking_on,
            thinking_budget=_thinking_budget,
            prior_reasoning=_prior_log[-1] if _prior_log else "",
            answered_so_far=_render_answered_so_far(messages),
            known_axes=list(state.get("grill_coverage_axes") or []),
        )
        await _emit("grill_composing_questions")
        batch = await _generate_grill_questions_llm(
            messages + new_messages, soul_prompt, context_block, task_id,
            failure=_grill_failure,
        )

    if batch is None:
        # No usable batch. Name the ACTUAL cause: an unreachable engine and a
        # model that answered with unusable JSON need opposite responses, and
        # reporting both as "can't reach the model" sent the user to restart an
        # engine that was already running. End the turn with hitl_pending=True,
        # which route_after_analyst routes to END — NOT the self-loop, which
        # would otherwise retry until the recursion limit.
        from api.websocket_manager import vfs_manager  # deferred: avoids circular import

        _notice = _grill_failure_message(_grill_failure)
        try:
            await vfs_manager.broadcast_token(task_id, _notice)
            await vfs_manager.broadcast_stream_end(task_id)
        except Exception as exc:  # noqa: BLE001 — a dead socket must not crash the graph
            logger.debug("AnalystAgent: grill-failure notice not delivered: %s", exc)
        degraded: Dict[str, Any] = {
            "hitl_pending": True,
            "shared_understanding_reached": False,
            "messages": new_messages + [{"role": "assistant", "content": _notice}],
            "grill_round_count": round_count + 1,
        }
        if dispatch_trace:
            degraded["tool_dispatch_trace"] = dispatch_trace
        return _with_reasoning(degraded)

    if not batch.questions:
        result: Dict[str, Any] = {
            "shared_understanding_reached": True,
            "hitl_pending": False,
            "messages": new_messages,
            "grill_round_count": round_count + 1,
        }
        if dispatch_trace:
            result["tool_dispatch_trace"] = dispatch_trace
        return _with_reasoning(result)

    # Commit the batch to state and return — do NOT resolve answers here. The
    # ask phase (top of this function, next self-loop visit) is where
    # _resolve_grill_answers runs, as its first action, against these exact
    # dicts read back from state. See the docstring for why generating and
    # interrupting in the same invocation is unsafe.
    committed: Dict[str, Any] = {
        "hitl_pending": False,
        "shared_understanding_reached": False,
        "messages": new_messages,
        "grill_round_count": round_count + 1,
        "pending_grill_batch": questions_to_pending_dicts(batch.questions),
    }
    # Append the executed-tool record only when the loop actually ran a tool, so
    # the read-only no-tool turn keeps its minimal state-delta contract.
    if dispatch_trace:
        committed["tool_dispatch_trace"] = dispatch_trace
    return _with_reasoning(committed)


async def _assemble_socratic_context(state: Dict[str, Any]) -> str:
    """Build the read-only workspace context block for a Socratic question.

    Reuses the analyst context assembler (active file + workspace tree + GraphRAG)
    so the grilling references the user's real code. The snippets must be passed
    in explicitly — the assembler retrieves nothing on its own, so omitting them
    silently empties the GraphRAG layer of the block. Never raises — a context
    failure degrades to an empty block, never crashes the graph node.
    """
    active_path: str = state.get("active_file_path") or ""
    paths: List[str] = [active_path] if active_path else []
    project_root: str = state.get("workspace_root") or ""
    project_id: Optional[str] = state.get("project_id") or None
    session_id: str = state.get("task_id", "")
    if not paths and not project_root:
        return ""
    try:
        from agents.analyst_context import assemble_analyst_context, fetch_intent_snippets
        # Per-round retrieval is affordable here: this node already pays a full
        # LLM tool-grounding loop each round, and a vector lookup is cheaper than
        # what it already spends.
        rag_snippets = await fetch_intent_snippets(
            state.get("user_input") or "", project_id, project_root
        )
        return await assemble_analyst_context(
            paths, project_id, session_id,
            rag_snippets=rag_snippets, project_root=project_root,
        )
    except Exception as exc:  # noqa: BLE001 — context assembly is best-effort
        logger.debug("Socratic context assembly failed (degrading): %s", exc)
        return ""


async def _gather_tool_grounding(
    state: Dict[str, Any],
    config: Optional[RunnableConfig],
    task_id: str,
) -> tuple[str, List[Dict[str, Any]]]:
    """Run a bounded READ_ONLY tool loop to ground the next Socratic question.

    The analyst may call its diagnostic tools (lint, complexity, diff, dependency
    audit) over the user's real code before asking, so the question references
    concrete findings rather than guessing. Every call is gated through the same
    permission matrix as the rest of the system; the analyst's tools are all
    READ_ONLY, so the gate is friction-free here. Best-effort: skipped when there
    is no workspace to inspect, and any failure degrades to no grounding (the
    analyst must never crash the graph).

    Returns the grounding text block (possibly empty) and the trace of executed
    tool calls (name + args per entry) for the state delta.
    """
    if not (state.get("workspace_root") or state.get("active_file_path")):
        # A silent no-op here previously left "the analyst ran no tools" with
        # no trace at all — indistinguishable from a grounding pass that simply
        # found nothing. One retrieval marker naming the skip reason makes the
        # absence itself visible on the Glass-Box Timeline.
        push_activity = (config or {}).get("configurable", {}).get("push_activity")
        if push_activity is not None:
            try:
                await push_activity("retrieval", metric="no workspace to ground against")
            except Exception:  # noqa: BLE001 — observability must never break the grill
                logger.debug("Analyst grounding-skip marker emit failed", exc_info=True)
        return "", []
    try:
        from core.permissions import session_mode_from_channel
        from core.tool_dispatch import ToolCall, ToolDispatcher, make_gateway_reasoner
        from shared.rbac import PermissionMode
        from tools.analyst_tools import build_analyst_tools

        tools = build_analyst_tools(state)
        dispatcher = ToolDispatcher(
            tools,
            active_role="analyst",
            session_mode=session_mode_from_channel(state.get("session_permission_mode")),
            state=state,
            agent_permission=PermissionMode.READ_ONLY,
        )
        configurable = (config or {}).get("configurable", {})
        reasoner = configurable.get("analyst_tool_reasoner") or make_gateway_reasoner(
            tools, session_id=task_id
        )
        seed = (
            "Before asking your next Socratic question, you MAY call READ_ONLY "
            "diagnostic tools to ground it in the user's real code. Call only what "
            "helps; emit {} to skip."
        )
        loop_messages: List[Dict[str, Any]] = [{"role": "user", "content": seed}]
        trace: List[ToolCall] = []
        await dispatcher.run_loop(
            loop_messages, reasoner, max_iters=_ANALYST_TOOL_MAX_ITERS, trace=trace
        )
        observations = [
            str(m.get("content", ""))
            for m in loop_messages
            if m.get("role") == "system"
            and str(m.get("content", "")).startswith("[tool observations]")
        ]
        block = (
            "## Read-only diagnostics gathered for this question\n"
            + "\n\n".join(observations)
            if observations
            else ""
        )
        return block, [{"name": c.name, "args": c.args} for c in trace]
    except Exception as exc:  # noqa: BLE001 — analyst must never crash the graph
        logger.warning(
            "Analyst tool grounding failed [%s: %s]", type(exc).__name__, exc
        )
        return "", []


def _build_grill_llm_messages(
    messages: List[Dict[str, Any]], soul_prompt: str, context_block: str
) -> List[Dict[str, str]]:
    """Assemble the system prompt (SOUL persona + Grill-Me JSON-output contract
    + language mirror + workspace context) and replay conversation history,
    folding StateSummarizer's compacted-history entry into the leading system
    message instead of dropping it (DEBT-181) — shared by the live grill call."""
    from agents.roles import LANGUAGE_MIRROR_DIRECTIVE
    from brain.summarizer import HISTORY_SUMMARY_PREFIX

    system_prompt = f"{soul_prompt}\n\n{_GRILL_DIRECTIVE}\n\n{LANGUAGE_MIRROR_DIRECTIVE}"
    if context_block:
        system_prompt = f"{system_prompt}\n\n{context_block}"

    llm_messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not content:
            continue
        if role in ("user", "assistant"):
            llm_messages.append({"role": role, "content": str(content)})
        elif role == "system" and str(content).startswith(HISTORY_SUMMARY_PREFIX):
            llm_messages[0]["content"] = (
                f"{llm_messages[0]['content']}\n\n## Earlier in this dialogue\n{content}"
            )
    return llm_messages


def _render_answered_so_far(messages: List[Dict[str, Any]]) -> str:
    """Flatten what the operator has already settled, for the reasoning prompt.

    Only the operator's own turns: the analyst's previous questions are already
    implied by the answers, and replaying both doubles the block for no added
    signal.
    """
    answers = [
        str(m.get("content", "")).strip()
        for m in messages
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]
    return "\n".join(f"- {a}" for a in answers)


def _parse_coverage_axes(reasoning: str) -> List[str]:
    """Pull the axes the model named out of its own closing line.

    Free prose, so this can legitimately find nothing — a model that ignored the
    marker, or a pass that was cut short by its token ceiling. An empty result is
    the documented degrade path, not an error: the caller falls back to the round
    counter, which is exactly today's behaviour.
    """
    marker_at = reasoning.rfind(_AXES_MARKER)
    if marker_at < 0:
        return []
    tail = reasoning[marker_at + len(_AXES_MARKER):].splitlines()[0]
    axes: List[str] = []
    for raw in tail.split(","):
        axis = raw.strip().strip(".;-*[]").strip()
        if axis and len(axis) <= _MAX_AXIS_CHARS and axis not in axes:
            axes.append(axis)
    return axes[:_MAX_COVERAGE_AXES]


async def _stream_grill_reasoning(
    *,
    user_input: str,
    context_block: str,
    soul_prompt: str,
    session_id: str,
    on_thinking: Optional[Callable[[str, str], Awaitable[None]]],
    thinking_on: bool,
    thinking_budget: int,
    prior_reasoning: str = "",
    answered_so_far: str = "",
    known_axes: Optional[List[str]] = None,
) -> str:
    """Stream the analyst's thinking to the Thought Box, and return what it said.

    A separate free-form completion rather than reasoning attached to the batch
    call: that call is a strict ``json_object`` contract, and a reasoning preamble
    corrupts a machine-parsed output. Mirrors the planner's pre-draft pass.

    Unlike the planner's, this runs for native models too. The planner can skip
    them because its draft is *streamed*, so a native model's own reasoning
    channel already reaches the user; the question batch is drafted with a
    non-streaming ``ainvoke``, where nothing surfaces on its own. Do not
    "restore" the native short-circuit here — it would silence the interview
    again on exactly the best models.

    The returned text is the round's actual reasoning, kept rather than discarded
    for three readers: the NEXT round (so it stops re-deriving what it already
    concluded — the whole reason consecutive rounds used to read alike), the
    distillation (which otherwise never sees where the analyst looked or what it
    ruled out), and the coverage stop.

    ``prior_reasoning`` is the immediately preceding round's text ONLY, never the
    accumulated log: the prompt must not grow per round, or it shrinks the output
    budget derived from the same window — the failure the planner's retry
    corrective had to be rewritten to avoid.

    Best-effort throughout: a sink or generation fault leaves the interview
    unchanged, and returns "". A real abort still propagates.
    """
    if on_thinking is None or not thinking_on:
        return ""

    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import

    sections: List[str] = [
        f"The developer asked: '{user_input}'.",
        f"What is already known about their workspace:\n{context_block}",
    ]
    if answered_so_far:
        sections.append(f"What they have already told you:\n{answered_so_far}")
    if prior_reasoning:
        # Negative, not prescriptive: it says what is already settled, never what
        # to examine next. A fixed set of angles would make every task's interview
        # the same interview, which is the failure mode this whole pass exists to
        # break out of.
        sections.append(
            "You already reasoned this far in the previous round:\n"
            f"{prior_reasoning}\n\n"
            "Do not restate any of it. Take the thinking FURTHER — what does the "
            "developer's answer above open up, contradict, or leave unresolved?"
        )
    if known_axes:
        sections.append(
            "Dimensions you named earlier as mattering here: "
            + ", ".join(known_axes)
            + ". Note which are now settled and which are still open."
        )
    sections.append(
        "Think out loud about what is still genuinely ambiguous, in concise "
        "conceptual prose — no questions yet, no code, no JSON.\n"
        f"Close with one final line, exactly '{_AXES_MARKER} a, b, c', naming the "
        "dimensions this particular task turns on — whatever they actually are "
        "for this work. If every dimension you name is now settled, close with "
        f"'{_AXES_MARKER} none'."
    )
    messages = [
        {"role": "system", "content": soul_prompt},
        {"role": "user", "content": "\n\n".join(sections)},
    ]

    captured: List[str] = []
    sink_live = True
    try:
        async for delta in LLMGateway.astream_reasoning(
            messages,
            tier=_GRILL_TIER,
            temperature=_GRILL_REASONING_TEMPERATURE,
            max_tokens=_GRILL_REASONING_MAX_TOKENS,
            session_id=session_id,
            thinking_budget_tokens=thinking_budget,
            free_form_answer=True,
        ):
            # The whole free-form output is reasoning, so both 'thinking' and
            # 'text' deltas belong in the Thought Box.
            if delta.text:
                captured.append(delta.text)
            if sink_live and delta.text:
                try:
                    await on_thinking(delta.text, delta.source)
                except asyncio.CancelledError:
                    raise
                except Exception as sink_exc:  # noqa: BLE001 — best-effort stream
                    logger.debug(
                        "grill reasoning sink failed; latching off: %s", sink_exc
                    )
                    sink_live = False
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — reasoning never blocks the interview
        logger.debug("grill reasoning pass failed (non-fatal)", exc_info=True)
    # Whatever streamed before a mid-flight fault is still real reasoning; the
    # partial text is worth more to the next round than an empty string.
    return "".join(captured).strip()


def _is_transport_failure(exc: BaseException) -> bool:
    """True when ``exc`` means the engine could not be reached at all.

    Separates "no engine answered" from "an engine answered with something
    unusable". Only the first is fixed by starting a model; reporting the second
    that way sends the reader to the wrong place entirely.
    """
    import httpx

    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    try:
        import litellm

        return isinstance(
            exc,
            (
                litellm.exceptions.APIConnectionError,
                litellm.exceptions.Timeout,
                litellm.exceptions.ServiceUnavailableError,
                litellm.exceptions.InternalServerError,
            ),
        )
    except Exception:  # noqa: BLE001 — litellm shape varies by version; default to "answered"
        logger.debug("transport-failure classification degraded", exc_info=True)
        return False


def _is_provider_unavailable(exc: BaseException) -> bool:
    """True when a reachable provider refused this call *for now*.

    A narrower reading of the transport bucket: the endpoint answered, but with
    a capacity/quota refusal (503, 429, 5xx) rather than a result. It needs the
    opposite response from an endpoint nothing is listening on — waiting or
    using another tier fixes it, starting a local engine does not. Retrying the
    same target immediately cannot help either, which is why the caller backs
    off and fails over instead.
    """
    try:
        import litellm

        return isinstance(
            exc,
            (
                litellm.exceptions.ServiceUnavailableError,
                litellm.exceptions.RateLimitError,
                litellm.exceptions.InternalServerError,
            ),
        )
    except Exception:  # noqa: BLE001 — litellm shape varies by version; default to "not a refusal"
        logger.debug("provider-unavailable classification degraded", exc_info=True)
        return False


# Pause before re-issuing a call a provider refused for capacity reasons. A
# retry with no delay reaches the same overloaded backend and fails identically;
# this is short enough not to strand an interactive turn, long enough to clear a
# momentary spike.
_GRILL_RETRY_BACKOFF_S: float = 1.5


async def _generate_grill_questions_llm(
    messages: List[Dict[str, Any]],
    soul_prompt: str,
    context_block: str,
    session_id: str,
    failure: Optional[Dict[str, str]] = None,
) -> Optional[GrillQuestionBatch]:
    """One structured call producing every question the analyst currently needs
    answered, batched together (replaces the old one-question-per-turn stream —
    a JSON batch isn't stream-friendly, matching how the planner's own
    structured plan generation already works non-streaming).

    Follows the established prompt-JSON pattern (`LLMGateway._extract_nested_schema_target`
    + `model_validate`, mirroring `agents/planner.py`'s retry-with-correction
    shape): retries once on a validation failure with the error folded back in.

    Returns ``None`` when no chat target resolves or the model never produced a
    valid batch — distinct from ``GrillQuestionBatch(questions=[])``, which is the
    model's deliberate "I have enough, hand off" signal. Those two causes need
    OPPOSITE responses from the user (start an engine, versus the model answered
    but malformed), so the failure is also recorded in ``failure`` for the caller
    to report accurately; ``None`` alone conflated them and reported a schema
    failure as an unreachable engine. Never raises — the analyst must never crash
    the graph.
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from core.config.model_resolver import get_chat_target, get_failover_target

    # Resolve the BYOM target up front. Without this, the invoke below silently
    # falls back to the litellm proxy when no preset is active and burns the full
    # transport-retry budget before failing — the streaming path this replaced
    # raised NoAvailableProviderError immediately instead.
    if get_chat_target(_GRILL_TIER) is None:
        logger.warning("AnalystAgent: no active BYOM chat model — cannot run the grill.")
        if failure is not None:
            failure["reason"] = "unreachable"
        return None

    llm_messages = _build_grill_llm_messages(messages, soul_prompt, context_block)
    # The configured tier stays the preferred target; a failover only ever supplies
    # an ADDITIONAL attempt, so a healthy preset never silently runs elsewhere.
    model = _GRILL_MODEL
    attempted_failover = False

    for attempt in range(2):
        try:
            response = await LLMGateway.ainvoke(
                messages=llm_messages,
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                session_id=session_id,
            )
            raw_content = response.choices[0].message.content or ""
            parsed = LLMGateway._extract_nested_schema_target(raw_content, GrillQuestionBatch)
            return GrillQuestionBatch.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 — retried once, then degrades below
            logger.warning(
                "AnalystAgent grill-question generation failed attempt=%d model=%s [%s: %s]",
                attempt + 1, model, type(exc).__name__, exc, exc_info=True,
            )
            unavailable = _is_provider_unavailable(exc)
            if failure is not None:
                # Three outcomes, three different user actions: a provider that
                # refused for capacity (wait / switch tier), an endpoint nothing
                # answered on (start the engine), and a model that answered
                # unusably (use a larger tier). Collapsing the first two sent the
                # user to restart a local engine over a cloud rate limit.
                failure["reason"] = (
                    "unavailable" if unavailable
                    else "unreachable" if _is_transport_failure(exc)
                    else "malformed"
                )
                failure["detail"] = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                if unavailable:
                    # Same overloaded backend on an immediate retry fails identically:
                    # pause, then prefer a different target if the preset declares one.
                    await asyncio.sleep(_GRILL_RETRY_BACKOFF_S)
                    _current = get_chat_target(_GRILL_TIER)
                    _next = (
                        get_failover_target(_GRILL_TIER, exclude_model=_current.model)
                        if _current is not None else None
                    )
                    if _next is not None:
                        model = _next.model
                        attempted_failover = True
                        logger.warning(
                            "AnalystAgent: %s unavailable — retrying the batch on %s.",
                            _GRILL_TIER, model,
                        )
                else:
                    # Only a malformed answer earns the corrective: telling a model
                    # its JSON was invalid when the call never reached it is a false
                    # accusation that also poisons the retry's own prompt.
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            f"That response was not valid JSON matching the required "
                            f"shape. Error: {exc}. Respond again with ONLY the JSON object."
                        ),
                    })
    logger.warning(
        "AnalystAgent: grill-question generation exhausted retries (failover=%s).",
        attempted_failover,
    )
    return None


_ANALYST_BYOM_DOWN: str = (
    "I can't reach the configured model right now. Activate a BYOM preset "
    "(Dashboard → BYOM) and make sure its engine is running, then ask me again."
)

_ANALYST_GRILL_MALFORMED: str = (
    "The model replied, but not with the structured questions I need to run the "
    "interview — this usually means the configured model is too small for a "
    "strict-JSON task. Try a larger tier, or send your request again to plan "
    "without the interview."
)

_ANALYST_GRILL_UNAVAILABLE: str = (
    "The model provider turned this request away — it is rate-limited or "
    "temporarily overloaded, not offline. I already retried on another tier. "
    "Send your request again in a moment, or point the interview tier at a "
    "model that is free right now (Dashboard → BYOM)."
)


def _grill_failure_message(failure: Dict[str, str]) -> str:
    """The user-facing notice for a grill that produced no batch.

    ``failure["reason"]`` separates three causes with three different fixes: a
    provider refusing for capacity (wait or switch tier), an endpoint nothing
    answered on (start the engine), and a model that answered unusably (use a
    larger tier). One message for all of them actively misdirects — a cloud rate
    limit read as "start your engine" sends the user to a local process that was
    never involved.
    """
    reason = failure.get("reason")
    detail = failure.get("detail")
    if reason == "unavailable":
        if detail:
            logger.info("AnalystAgent: grill provider unavailable — %s", detail)
        return _ANALYST_GRILL_UNAVAILABLE
    if reason == "malformed":
        if detail:
            logger.info("AnalystAgent: grill batch unusable — %s", detail)
        return _ANALYST_GRILL_MALFORMED
    return _ANALYST_BYOM_DOWN


def _analyst_failure_message(exc: BaseException) -> str:
    """Map a failed analyst turn to a message naming its ACTUAL cause.

    A local-memory refusal and an unreachable engine need opposite responses
    from the user (free RAM / pick a smaller tier, versus start the engine), so
    reporting both as "can't reach the model" sends them to the wrong fix. The
    resource error already carries the numbers that make it actionable.
    """
    from core.config.model_resolver import LocalResourceExhaustedError

    if isinstance(exc, LocalResourceExhaustedError):
        return str(exc)
    return _ANALYST_BYOM_DOWN


async def generate_analyst_reply_stream(
    text: str,
    context_block: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    session_id: str = "",
    tier: str = "medium",
    on_reasoning: Optional[Callable[[str, str], Awaitable[None]]] = None,
    enable_thinking: bool = True,
) -> AsyncIterator[str]:
    """ streaming analyst reply for the Natt pane.

    System prompt = SOUL persona (already identity) + the assembled,
    budgeted, sandboxed analyst context block. Conversation memory (history) is replayed
    so the analyst keeps continuity. ``tier`` selects the answer model from the active BYOM
    preset (the user trades speed vs quality); it does not affect retrieval. Outbound answer
    tokens are coalesced into chunk_ms=40 frames via the shared batcher. Degrades to one
    actionable message if the BYOM engine is down — the analyst must never crash the WS loop.
    Read-only.

    When ``enable_thinking`` and a reasoning sink is wired, the reply is produced through
    the shared reasoning-aware engine: the model's reasoning (native or a scaffolded
    simulation) streams to ``on_reasoning(delta, source)`` while only the answer is batched
    to the caller. The reasoning sink is best-effort — a dead sink never aborts the reply.
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from transport.token_batcher import batch_tokens

    system_prompt = soul_manager.get_prompt()
    if context_block:
        system_prompt = f"{system_prompt}\n\n{context_block}"

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})

    async def _answer_only() -> AsyncIterator[str]:
        """Yield only answer text; route reasoning deltas to the sink (best-effort)."""
        async for d in LLMGateway.astream_reasoning(
            messages, tier=tier, temperature=0.4, session_id=session_id,
            # Natt replies are free markdown, never machine-parsed — safe to
            # scaffold (see astream_reasoning's SAFETY INVARIANT).
            free_form_answer=True,
        ):
            if d.kind == "thinking":
                if on_reasoning is not None:
                    try:
                        await on_reasoning(d.text, d.source)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — reasoning sink is best-effort
                        logger.debug("analyst reasoning sink failed (non-fatal): %s", exc)
            else:  # "text" — the answer channel
                yield d.text

    try:
        if enable_thinking and on_reasoning is not None:
            raw: AsyncIterator[str] = _answer_only()
        else:
            raw = LLMGateway.astream_byom(messages, tier=tier, session_id=session_id)
        produced = False
        async for chunk in batch_tokens(raw, chunk_ms=40):
            produced = True
            yield chunk
        if not produced:
            yield "(no response)"
    except Exception as exc:  # noqa: BLE001 — analyst must never crash the WS loop
        logger.warning(
            "Analyst live reply failed [%s: %s]", type(exc).__name__, exc, exc_info=True
        )
        yield _analyst_failure_message(exc)


async def generate_analyst_reply(text: str, session_id: str = "") -> str:
    """full (non-streaming) analyst reply for the Natt pane.

    Backward-compatible single-string entry point, now backed by the streaming
    generator (no context wiring; callers wanting context use stream_analyst_reply).
    """
    parts: List[str] = []
    async for chunk in generate_analyst_reply_stream(text, session_id=session_id):
        parts.append(chunk)
    return "".join(parts).strip() or "(no response)"


# ---------------------------------------------------------------------------
# Nightmare Protocol
# ---------------------------------------------------------------------------

_NIGHTMARE_SYSTEM_PROMPT: str = (
    "You are the Nightmare Judge for AILIENANT. Given a code delta and a "
    "list of project rules, score the delta on a 0.0-1.0 scale where:\n"
    "  1.0 = no rules violated, clean diff\n"
    "  0.5 = stylistic concerns or weak adherence\n"
    "  0.0 = at least one hard rule is violated\n"
    "Respond with ONLY a JSON object of the form: "
    '{"reward": <float in [0.0, 1.0]>, "violated_rules": [<rule strings>]}'
    "\nNo prose, no markdown fences. If unsure, default to reward 0.0."
)


class NightmareEvaluation(BaseModel):
    """Pydantic result of one Nightmare Protocol evaluation."""

    reward: float = Field(ge=0.0, le=1.0)
    violated_rules: List[str] = Field(default_factory=list)


_NIGHTMARE_FAILSAFE: NightmareEvaluation = NightmareEvaluation(
    reward=0.0, violated_rules=["LLM_EVAL_FAILED"],
)


def _parse_nightmare_response(raw_content: Optional[str]) -> NightmareEvaluation:
    """Parse the JSON body of a Nightmare/SupremeJudge response. Failsafe on bad input.

    routes through the gateway's envelope unwrapper so a wrapped
    verdict ({"result": {…}}, fenced, or prose-prefixed) is still scored instead of
    failsafing to 0.0. Returns the failsafe only when the text is genuinely unparseable.
    """
    if raw_content is None:
        return _NIGHTMARE_FAILSAFE
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    parsed = LLMGateway._extract_nested_schema_target(raw_content, NightmareEvaluation)
    if not parsed:
        return _NIGHTMARE_FAILSAFE
    try:
        clamped_reward: float = max(0.0, min(1.0, float(parsed.get("reward", 0.0))))
        violated = parsed.get("violated_rules", [])
        if not isinstance(violated, list):
            violated = []
        return NightmareEvaluation(
            reward=clamped_reward,
            violated_rules=[str(v) for v in violated],
        )
    except (TypeError, ValueError):
        return _NIGHTMARE_FAILSAFE


async def evaluate_nightmare(
    code_delta: str,
    rules_json_path: str,
    session_id: str = "",
) -> NightmareEvaluation:
    """Score a code delta against the project's .ailienant.json rules.

    rules_json_path is the workspace directory containing .ailienant.json
    (matches RuleManager.get_combined_rules() contract). Failsafe on any
    error: returns reward=0.0 + violated_rules=["LLM_EVAL_FAILED"] so a
    broken judge never green-lights rule-violating code.
    """
    from tools.llm_gateway import LLMGateway   # deferred — avoids circular import
    from shared.config import MINI_JUDGE_MODEL  # reuse the fast mini-judge model
    from core.rules import RuleManager

    rules_text: str = RuleManager().get_combined_rules(rules_json_path)
    user_payload: str = (
        f"### Project Rules:\n{rules_text}\n\n"
        f"### Code Delta:\n{code_delta}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _NIGHTMARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=120,
            session_id=session_id,
        )
        raw_content = response.choices[0].message.content
        result = _parse_nightmare_response(raw_content)
        logger.info(
            "Nightmare: delta_len=%d reward=%.3f n_violations=%d",
            len(code_delta), result.reward, len(result.violated_rules),
        )
        return result
    except Exception as exc:
        logger.warning("Nightmare: LLM eval failed (failsafe reward=0.0): %s", exc)
        return _NIGHTMARE_FAILSAFE


# ---------------------------------------------------------------------------
# Supreme Judge (Tier.CLOUD reward evaluation)
# ---------------------------------------------------------------------------

async def supreme_judge_evaluate(
    code_delta: str,
    rules_json_path: str,
    session_id: str = "",
) -> NightmareEvaluation:
    """Tier.CLOUD reward evaluation for MCTS rollouts.

    Identical contract to evaluate_nightmare() but routes via Tier.CLOUD
    (MODEL_BIG) for higher-quality reasoning. Called only after the local
    Micro-Isolate pipeline passes (see agents/mcts_coder.py).
    """
    from tools.llm_gateway import LLMGateway, Tier
    from core.rules import RuleManager

    rules_text: str = RuleManager().get_combined_rules(rules_json_path)
    user_payload: str = (
        f"### Project Rules:\n{rules_text}\n\n"
        f"### Code Delta:\n{code_delta}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _NIGHTMARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            tier=Tier.CLOUD,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=120,
            session_id=session_id,
        )
        raw_content = response.choices[0].message.content
        result = _parse_nightmare_response(raw_content)
        logger.info(
            "SupremeJudge: delta_len=%d reward=%.3f n_violations=%d",
            len(code_delta), result.reward, len(result.violated_rules),
        )
        return result
    except Exception as exc:
        logger.warning("SupremeJudge: LLM eval failed (failsafe reward=0.0): %s", exc)
        return _NIGHTMARE_FAILSAFE


# ---------------------------------------------------------------------------
# Rule Distillation
# ---------------------------------------------------------------------------

_RULE_DISTILLER_SYSTEM_PROMPT: str = (
    "You are the AnalystAgent performing Rule Distillation. "
    "You will receive CODE_A (what the AI wrote) and CODE_B (what the human "
    "corrected it to). Deduce ONE concise project rule (<=20 words) that, if "
    "the AI had followed it, would have made it write CODE_B in the first place. "
    "Focus on the underlying coding preference, not the literal edit.\n"
    "Examples: 'Prefer list comprehensions over for-loop accumulation'; "
    "'Use single quotes for string literals'; 'Type-annotate all public functions'.\n"
    "If the diff is purely cosmetic (whitespace, trivial naming) or no clear "
    "rule emerges, respond {\"rule\": null}. "
    'Respond ONLY with a JSON object: {"rule": "<rule>"} or {"rule": null}.'
)


async def distill_rejection_to_rule(
    original_code: str,
    user_code: str,
    session_id: str = "",
) -> Optional[str]:
    """Diff AI vs human; ask the mini-judge to extract one coding rule.

    Returns the rule string or None (LLM declined / trivial diff / failure).
    Never raises — telemetry must not block the user.
    """
    if not original_code.strip() or not user_code.strip():
        return None
    if original_code == user_code:
        return None
    from tools.llm_gateway import LLMGateway   # deferred — circular guard
    from shared.config import MINI_JUDGE_MODEL
    user_payload: str = (
        f"### CODE_A (AI wrote):\n{original_code}\n\n"
        f"### CODE_B (human corrected):\n{user_code}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _RULE_DISTILLER_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=80,
            session_id=session_id,
        )
        raw = response.choices[0].message.content
        if raw is None:
            return None
        parsed = json.loads(raw)
        rule = parsed.get("rule")
        if rule is None:
            return None
        rule_str: str = str(rule).strip()
        if not rule_str or rule_str.lower() == "none":
            return None
        logger.info("RuleDistiller: extracted rule (%d chars)", len(rule_str))
        return rule_str
    except Exception as exc:
        logger.warning("RuleDistiller: LLM failed (skipping): %s", exc)
        return None
