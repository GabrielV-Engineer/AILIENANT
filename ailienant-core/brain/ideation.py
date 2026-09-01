# ailienant-core/brain/ideation.py
#
# Phase 2.21 — Socratic Ideation Sub-Graph.
#
# Compiled without a checkpointer — the parent graph's CheckpointManager
# (brain/checkpoint.py / brain/engine.py) handles all persistence.
#
# Node topology:
#   analyst_grill → [route_after_analyst]
#       shared_understanding_reached=True  → synthesis_node → END (handoff to planner)
#       hitl_pending=True                  → END (degraded — no reachable model)
#       shared_understanding_reached=False → analyst_grill (another round — the human's
#           answer for the round just run is already resolved via native
#           interrupt()/resume inside the node itself before it returns, so there is no
#           "await the next top-level turn" outcome left to route to END for; the actual
#           pause lives inside interrupt(), not at this edge)
#
# synthesis_node does NOT draft the plan. It distills the dialogue into a brief and
# hands off to the autonomous PlannerAgent (engine.route_after_ideation), whose
# Actor-Critic reflection loop produces the schema-valid WBS. Compressing ambiguous
# dialogue straight into the rigid MissionSpecification in one zero-shot call is a
# single P(E) failure point that collapses on weak/quantized models; the planner's
# draft→validate→re-draft loop drives that to P(E)^n instead.

import json
import logging
import os as _os
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from brain.state import (
    AIlienantGraphState, accepts_config, assert_declared_channels, derive_node_role,
)
from core.activity_context import bind_agent_role, bind_model_tier, reset_agent_role, reset_model_tier

logger = logging.getLogger("IDEATION_GRAPH")

# Live distillation is the default; the placeholder stub is the deterministic
# escape hatch for CI/UI smoke tests. Mirrors planner/analyst:
# set AILIENANT_IDEATION_DEBUG=1 to force the stub.
DEBUG_MODE: bool = _os.getenv("AILIENANT_IDEATION_DEBUG", "0") != "0"

# `_distill_brief_llm` pins its call to MODEL_BIG (compressing the whole
# dialogue into the planner's brief is a high-blast-radius single-shot — see
# that function's own rationale). The tier is derived from that same alias so
# the two cannot drift apart.
from core.config.model_resolver import tier_for_alias  # noqa: E402
from shared.config import MODEL_BIG as _SYNTHESIS_MODEL  # noqa: E402
_SYNTHESIS_TIER: str = tier_for_alias(_SYNTHESIS_MODEL, default="big")

# Distill the Socratic dialogue into a SOFT brief — intent + hard constraints +
# domain glossary. Deliberately NOT the rigid MissionSpecification: a missing field
# degrades gracefully, so there is no schema gamble here. The downstream PlannerAgent
# turns this brief into the validated WBS under its reflection loop.
# Wire value tagging the brief-review approval so the frontend can render its own
# card. `request_kind` is an open Optional[str] on the payload, so a new value is
# additive by construction — no existing consumer changes (SCHEMA_EVOLUTION.MD §10).
BRIEF_REVIEW_KIND: str = "BRIEF_REVIEW"

# Rewrites of one brief before the current text is handed off regardless. Matches
# the grill's own round cap: each revision costs a full MODEL_BIG distillation, and
# a brief still wrong after this many corrections needs a new turn, not another
# pass over the same dialogue.
_BRIEF_MAX_REVISIONS: int = 3

# Output ceiling for the distillation. Derived from the planner's own declared
# ceiling rather than restated as a second literal (§5.7): the brief IS the
# planner's input, so a ceiling below the planner's would cap the requirement
# statement under the budget of the thing that reads it, and the two would drift
# the moment either was tuned alone. Like the planner's, this is the DECLARED
# ceiling — `_resolve_distill_budget` reconciles it against the real window.
from agents.planner import _PLANNER_DRAFT_MAX_MAX_TOKENS as _DISTILL_MAX_MAX_TOKENS  # noqa: E402


def _gateway_default_max_tokens() -> int:
    """The allowance `LLMGateway.ainvoke` applies when a caller passes none.

    Read from the signature rather than restated, so the degrade path below is
    provably the behaviour this call had before it was budgeted at all — and
    cannot silently diverge if the gateway's default moves. Falls back to the
    conservative context default if the signature is ever reshaped.
    """
    import inspect
    from tools.llm_gateway import LLMGateway
    from brain.agent_context import DEFAULT_CONTEXT_BUDGET

    param = inspect.signature(LLMGateway.ainvoke).parameters.get("max_tokens")
    default = getattr(param, "default", inspect.Parameter.empty)
    return default if isinstance(default, int) else DEFAULT_CONTEXT_BUDGET


# What the analyst writes in place of an axis list once nothing is left open. A
# small set rather than one literal because this is matched against free prose:
# the instruction asks for "none", and a model that answers the equivalent word
# means the same thing. Anything outside the set is read as a real axis, which
# keeps the safe direction — an unrecognised word costs one more round, while a
# loose match would end the interview early.
_SETTLED_AXES_SENTINELS: frozenset[str] = frozenset({"none", "nothing", "n/a", "-"})

_DISTILL_SYSTEM_PROMPT: str = (
    "You are the AnalystAgent closing a Socratic planning dialogue. The interview "
    "you just ran EARNED context — answers, and your own investigation of the "
    "developer's real code. Your job is to hand the planner everything of substance "
    "that came out of it, NOT to summarise it.\n"
    "Return a single JSON object (no prose, no markdown fences):\n"
    '{\n'
    '  "verbatim_requirements": ["<each concrete thing the developer specified, in '
    'THEIR words>"],\n'
    '  "intent": "<what to build and what done looks like — as long as the work '
    'actually requires>",\n'
    '  "constraints": ["<limits and rules the dialogue settled>"],\n'
    '  "scope_hints": ["<files/areas in or out of scope, if named>"],\n'
    '  "findings": ["<what you learned about their codebase while investigating>"],\n'
    '  "open_questions": ["<anything still genuinely unresolved>"],\n'
    '  "ubiquitous_language": {"<term>": "<definition>"}\n'
    '}\n'
    "FIDELITY RULES — these outrank brevity everywhere:\n"
    "- Every specific the developer gave — a number, a name, an API, a path, a "
    "library, a format, an example, an edge case — is reproduced EXACTLY as they "
    "wrote it. Never paraphrase a stated requirement into a general description; "
    '"handle auth" is not an acceptable rendering of a named flow with named '
    "fields.\n"
    "- You ADD to what the developer said. You never REPLACE it with something "
    "shorter. If a detail does not fit a field, it belongs in "
    "verbatim_requirements.\n"
    "- Length is not a virtue in either direction: a small task stays small, and a "
    "detailed one stays detailed. Never drop a specific to make the brief tidier.\n"
    "- Do not invent a work breakdown, file edits, or steps — the planner does "
    "that. Capture only what the dialogue and your investigation actually "
    "established.\n"
    "Mirror the language of the dialogue."
)


def _reasoning_transcript(reasoning_log: List[str]) -> str:
    """Render the grill's own per-round reasoning as its own labelled section.

    Kept distinct from the dialogue on purpose: the distillation has to tell what
    the OPERATOR stated (which it must reproduce verbatim) from what the ANALYST
    deduced (which it may summarise). Flattening both into one transcript invites
    exactly the confusion the fidelity rules exist to prevent.

    This is context the interview already paid for — where the analyst looked,
    what it ruled out — and which reached the operator's screen and then went
    nowhere, since the transcript only ever carried user/assistant turns.
    """
    entries = [str(r).strip() for r in (reasoning_log or []) if str(r).strip()]
    if not entries:
        return ""
    rounds = "\n\n".join(f"[round {i}] {r}" for i, r in enumerate(entries, start=1))
    return f"### The analyst's own reasoning during the interview\n{rounds}"


def _dialogue_transcript(messages: List[Dict[str, Any]]) -> str:
    """Flatten the accumulated Q&A into a plain transcript for the distillation prompt.

    Includes StateSummarizer's (brain/summarizer.py) compacted-history entry when
    present — dropping it would silently erase everything the dialogue settled
    before the turns it evicted once a long grill outgrew its token budget
    (DEBT-181).
    """
    from brain.summarizer import HISTORY_SUMMARY_PREFIX

    lines: List[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not content:
            continue
        if role in ("user", "assistant"):
            speaker = "USER" if role == "user" else "ANALYST"
            lines.append(f"{speaker}: {content}")
        elif role == "system" and str(content).startswith(HISTORY_SUMMARY_PREFIX):
            lines.append(f"EARLIER CONTEXT: {content}")
    return "\n".join(lines)


def _bullets(brief: Dict[str, Any], key: str) -> str:
    """Render one list field as bullets, or "" when it is absent or empty."""
    items = [str(i).strip() for i in (brief.get(key) or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items)


def _compose_planner_brief(
    brief: Dict[str, Any], fallback: str, original_request: str = ""
) -> str:
    """Render the distilled brief into the ``user_input`` the planner reads.

    Two blocks with a visible boundary, because they carry different authority.
    THE REQUEST is what the operator actually asked for, reproduced word for word;
    the planner may not reinterpret it. WHAT THE INTERVIEW ESTABLISHED is
    everything the dialogue and the analyst's investigation ADDED on top.

    The split is what stops the brief from restating the request in weaker words.
    Before it, the distillation replaced the operator's own wording with a
    paraphrase and the original was gone — a precise request came out of the
    interview vaguer than it went in, which inverts the point of running one.

    Degrades field by field: a distillation that returns only ``intent`` still
    composes, and an empty brief falls back to the raw transcript.
    """
    sections: List[str] = []

    original = (original_request or "").strip()
    if original:
        sections.append(
            "## THE REQUEST (verbatim — authoritative)\n"
            "The developer asked for this. Treat it as the specification; do not "
            "reinterpret or generalise it. Where anything below appears to "
            "conflict with it, this wins.\n\n"
            f"{original}"
        )

    verbatim = _bullets(brief, "verbatim_requirements")
    if verbatim:
        sections.append(f"### Specifics they stated\n{verbatim}")

    detail: List[str] = []
    intent = str(brief.get("intent") or "").strip()
    if intent:
        detail.append(intent)
    for label, key in (
        ("Constraints", "constraints"),
        ("Scope", "scope_hints"),
        ("Established about the codebase", "findings"),
        ("Still open", "open_questions"),
    ):
        rendered = _bullets(brief, key)
        if rendered:
            detail.append(f"{label}:\n{rendered}")
    glossary = brief.get("ubiquitous_language") or {}
    if isinstance(glossary, dict) and glossary:
        detail.append("Glossary: " + "; ".join(f"{k} = {v}" for k, v in glossary.items()))

    if detail:
        header = (
            "## WHAT THE INTERVIEW ESTABLISHED (additional context)"
            if original
            else ""
        )
        sections.append("\n\n".join(([header] if header else []) + detail))

    # Nothing usable came back at all — hand over the raw material rather than an
    # empty requirement statement.
    if not sections:
        return (intent or fallback)
    return "\n\n".join(sections)


async def _resolve_brief_review(
    task_id: str,
    brief_text: str,
    config: Optional[RunnableConfig],
) -> Dict[str, Any]:
    """Suspend for the operator's decision on the drafted brief.

    Mirrors ``agents/analyst.py::_resolve_grill_answers``: an injected
    ``brief_review_fn`` seam so the node stays unit-testable outside a live graph
    run (native ``interrupt()`` requires a runnable context), else the real
    suspend on the approval channel every other HITL card already uses.
    """
    review_fn = (config or {}).get("configurable", {}).get("brief_review_fn")
    if review_fn is not None:
        return dict(await review_fn(brief_text) or {})
    from core.hitl import request_graph_approval  # deferred — avoids import cycle

    return request_graph_approval(
        session_id=task_id,
        action_description=(
            "Review the brief distilled from your dialogue. This is the exact "
            "requirement statement the planner will work from."
        ),
        proposed_content=brief_text,
        request_kind=BRIEF_REVIEW_KIND,
    )


async def run_synthesis_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """LangGraph node: distill the Socratic dialogue, then hand off to the planner.

    This node does not produce a plan. It compresses the conversation into a soft
    brief, folds it into ``user_input``, and sets ``ideation_synthesized`` so the
    parent graph routes the turn into the autonomous PlannerAgent — whose
    draft→validate→re-draft loop produces the schema-valid WBS. ``mission_spec`` is
    intentionally left for the planner to own.

    The distillation is the one lossy step in the pipeline nothing else checks: it
    REPLACES ``user_input``, the planner's critic validates the resulting plan
    against a schema rather than against the dialogue, and a dropped constraint
    surfaces as an absence, which no downstream view can render. So the brief is
    shown to the operator before the handoff, over two graph super-steps (the
    self-loop edge on ``synthesis_node`` drives both):

      1. **Draft phase** (``pending_brief`` empty): distils, composes, commits to
         ``pending_brief`` and returns — no ``interrupt()`` in this super-step.
      2. **Review phase** (``pending_brief`` set): suspends on the STATE-SOURCED
         brief as the node's first action, then applies the decision.

    The split is load-bearing for the same reason ``pending_grill_batch``'s is, and
    costs more here: LangGraph replays a node from the top on every resume, so
    drafting and interrupting in one invocation would re-run the MODEL_BIG
    distillation on every review round — charging for it again and swapping out the
    very text under review, non-deterministically, for one the operator never saw.
    """
    messages: List[Dict[str, Any]] = state.get("messages", [])
    task_id: str = state.get("task_id", "")
    _narrate = (config or {}).get("configurable", {}).get("narrate")

    async def _emit(node_name: str) -> None:
        if _narrate is not None:
            await _narrate(node_name)

    # ── Review phase ──────────────────────────────────────────────────────
    pending: Optional[Dict[str, Any]] = state.get("pending_brief")
    revisions: int = int(state.get("brief_revision_count", 0) or 0)
    if pending:
        brief_text = str(pending.get("composed") or "")
        decision = await _resolve_brief_review(task_id, brief_text, config)
        edited = decision.get("modified_content")
        comment = str(decision.get("comment") or "").strip()

        if decision.get("approved"):
            # An in-place edit is authoritative: what the operator read and
            # corrected is what the planner must receive, not the draft behind it.
            final_brief = str(edited).strip() if edited and str(edited).strip() else brief_text
            _gloss = pending.get("glossary") or {}
            glossary = {str(k): str(v) for k, v in _gloss.items()} if isinstance(_gloss, dict) else {}
            logger.info(
                "SynthesisNode: brief accepted (%d char(s), edited=%s). "
                "Handing off to the autonomous PlannerAgent.",
                len(final_brief), bool(edited),
            )
            await _emit("handoff_to_planner")
            return {
                "user_input": final_brief,
                "ideation_glossary": glossary,
                "ideation_synthesized": True,
                "planner_mode_active": False,
                "shared_understanding_reached": True,
                "hitl_pending": False,
                "pending_brief": None,
                "brief_revision_note": None,
            }

        if comment:
            # Rewrite: the operator is correcting the SUMMARY, not supplying missing
            # information, so this re-distils the same dialogue under their steer
            # rather than re-entering the grill — which would also collide with its
            # own round cap, already spent by the time this node runs.
            if revisions >= _BRIEF_MAX_REVISIONS:
                # Every sibling loop in this graph carries an explicit bound; without
                # one here a repeatedly-corrected brief re-drafts until LangGraph's
                # global recursion limit, burning a MODEL_BIG distillation per pass
                # and surfacing as an opaque graph error rather than a clear stop.
                logger.warning(
                    "SynthesisNode: brief revision cap (%d) reached — handing the "
                    "current brief to the planner instead of re-drafting again.",
                    _BRIEF_MAX_REVISIONS,
                )
                _gloss_capped = pending.get("glossary") or {}
                await _emit("handoff_to_planner")
                return {
                    "user_input": brief_text,
                    "ideation_glossary": (
                        {str(k): str(v) for k, v in _gloss_capped.items()}
                        if isinstance(_gloss_capped, dict) else {}
                    ),
                    "ideation_synthesized": True,
                    "planner_mode_active": False,
                    "shared_understanding_reached": True,
                    "hitl_pending": False,
                    "pending_brief": None,
                    "brief_revision_note": None,
                }
            logger.info("SynthesisNode: brief sent back for a rewrite (%d char note).", len(comment))
            return {
                "pending_brief": None,
                "brief_revision_note": comment,
                "brief_revision_count": revisions + 1,
            }

        # Cancelled with nothing to act on. End the turn rather than guessing:
        # `messages` still holds the whole dialogue, so the operator's next turn
        # continues this thread instead of starting the interview over.
        #
        # A rewrite whose note was empty arrives here indistinguishably — the wire
        # carries only approved=false with no comment. The UI refuses that action
        # (briefReviewLogic.canSendBriefBack), so reaching this branch means a real
        # cancel; logged as such rather than silently.
        logger.info("SynthesisNode: brief review cancelled — ending the turn.")
        return {"pending_brief": None, "brief_revision_note": None, "hitl_pending": True}

    # ── Draft phase ───────────────────────────────────────────────────────
    # Bind-and-forget — `_guarded`'s `finally` owns cleanup, same contract as
    # `agents/analyst.py`'s `_GRILL_TIER` bind.
    bind_model_tier(_SYNTHESIS_TIER)
    await _emit("synthesizing_intent")

    revision_note = state.get("brief_revision_note")
    fallback_intent = _dialogue_transcript(messages) or (state.get("user_input") or "")
    # The operator's own wording, preserved by task_service before anything could
    # overwrite it. Absent on a checkpoint written before the channel existed, in
    # which case the brief composes exactly as it did then.
    original_request = str(state.get("original_user_request") or "")
    if DEBUG_MODE:
        brief: Dict[str, Any] = {"intent": fallback_intent}
        planner_brief = _compose_planner_brief(brief, fallback_intent, original_request)
    else:
        brief = await _distill_brief_llm(state, messages, revision_note=revision_note)
        planner_brief = _compose_planner_brief(brief, fallback_intent, original_request)

    _raw_gloss = brief.get("ubiquitous_language") or {}
    glossary = (
        {str(k): str(v) for k, v in _raw_gloss.items()} if isinstance(_raw_gloss, dict) else {}
    )

    logger.info(
        "SynthesisNode: distilled brief from %d message(s) (%d char(s), revised=%s). "
        "Awaiting the operator's review.",
        len(messages), len(planner_brief), bool(revision_note),
    )

    return {
        "pending_brief": {"composed": planner_brief, "glossary": glossary},
        "brief_revision_note": None,
    }


async def _resolve_distill_budget(state: Dict[str, Any], user_payload: str) -> int:
    """Output allowance for the distillation, sized against the real served window.

    Same mechanism the planner and coder use — measure the real prompt, probe the
    real window, take the smaller of the ceiling and what actually fits. The
    declared ceiling is the planner's, deliberately: the brief is the planner's
    input, and a step that must not drop detail cannot be allowed a fraction of
    the budget of the step that consumes it.

    Unlike the planner, an insufficient budget does NOT refuse here. The
    distillation's contract is that it never blocks the handoff, so a hopeless
    budget degrades to the gateway default and lets the (possibly truncated)
    result flow — a shorter brief still beats an ideation turn that dead-ends.
    """
    from brain.agent_context import resolve_output_budget, resolve_real_window
    from tools.token_counter import PrecisionTokenCounter
    from shared.config import MODEL_BIG

    try:
        real_window = await resolve_real_window(state, tier=_SYNTHESIS_TIER)
        prompt_tokens = PrecisionTokenCounter.estimate_with_buffer(
            f"{_DISTILL_SYSTEM_PROMPT}\n{user_payload}", MODEL_BIG
        )
        decision = resolve_output_budget(
            prompt_tokens=prompt_tokens,
            real_window=real_window,
            declared_ceiling=_DISTILL_MAX_MAX_TOKENS,
        )
        if decision.ok:
            return decision.max_tokens
        logger.warning(
            "SynthesisNode: output budget too tight (%s) — falling back to the "
            "gateway default; the brief may be shortened.", decision.reason,
        )
    except Exception:  # noqa: BLE001 — budgeting must never block the handoff
        logger.debug("SynthesisNode: budget resolution failed; using the default.",
                     exc_info=True)
    return _gateway_default_max_tokens()


async def _distill_brief_llm(
    state: Dict[str, Any],
    messages: List[Dict[str, Any]],
    revision_note: Optional[str] = None,
) -> Dict[str, Any]:
    """Soft-schema distillation of the dialogue into an intent/constraints brief.

    Grounds the brief in the workspace (active file + GraphRAG, best-effort). Never
    raises — a parse failure degrades to an intent-only brief so the handoff to the
    planner always proceeds (the planner's reflection loop carries the rigor).

    ``revision_note`` is the operator's correction after reading a previous draft.
    It rides in the USER payload, never in the system prompt, so the system message
    stays byte-identical across drafts.

    The output budget is reconciled against the model's REAL served window rather
    than left to the gateway's generic default, which nothing here ever chose: the
    step that decides how much of the interview survives was running on a quarter
    of the allowance the planner consuming its output gets.
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from shared.config import MODEL_BIG

    transcript = _dialogue_transcript(messages)
    context_block = await _assemble_synthesis_context(state)
    user_payload = transcript
    reasoning_block = _reasoning_transcript(list(state.get("grill_reasoning_log") or []))
    if reasoning_block:
        user_payload = f"{user_payload}\n\n{reasoning_block}"
    original_request = str(state.get("original_user_request") or "").strip()
    if original_request:
        # Stated first and named as authoritative: this is the text whose
        # specifics the fidelity rules require to survive intact.
        user_payload = (
            "### The developer's original request (reproduce its specifics exactly)\n"
            f"{original_request}\n\n{user_payload}"
        )
    if context_block:
        user_payload = f"{user_payload}\n\n### Workspace context\n{context_block}"
    if revision_note:
        user_payload = (
            f"{user_payload}\n\n### Correction to the previous brief\n"
            f"The operator read your previous brief and asked for this: {revision_note}\n"
            "Re-distil the same dialogue accordingly. Do not invent anything the "
            "dialogue did not settle."
        )

    session_id: str = state.get("task_id", "")
    try:
        max_tokens = await _resolve_distill_budget(state, user_payload)
        resp = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MODEL_BIG,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            session_id=session_id,
            state=state,
        )
        raw = LLMGateway._sanitize_json_response(resp.choices[0].message.content or "")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"intent": transcript}
    except Exception as exc:  # noqa: BLE001 — distillation must never crash the graph
        logger.warning("SynthesisNode: distillation failed (%s: %s); "
                       "handing the raw intent to the planner.",
                       type(exc).__name__, exc)
        return {"intent": transcript}


async def _assemble_synthesis_context(state: Dict[str, Any]) -> str:
    """Best-effort workspace context block to ground the distilled brief.

    The snippets are passed explicitly: the assembler retrieves nothing itself, so
    omitting them empties the GraphRAG layer of the block that becomes the
    planner's brief — the highest-leverage context in a plan session.
    """
    active_path: str = state.get("active_file_path") or ""
    paths: List[str] = [active_path] if active_path else []
    project_root: str = state.get("workspace_root") or ""
    project_id = state.get("project_id") or None
    if not paths and not project_root:
        return ""
    try:
        from agents.analyst_context import assemble_analyst_context, fetch_intent_snippets
        rag_snippets = await fetch_intent_snippets(
            state.get("user_input") or "", project_id, project_root
        )
        return await assemble_analyst_context(
            paths, project_id, state.get("task_id", ""),
            rag_snippets=rag_snippets, project_root=project_root,
        )
    except Exception as exc:  # noqa: BLE001 — context is best-effort
        logger.debug("Synthesis context assembly failed (degrading): %s", exc)
        return ""


def route_after_synthesis(state: Dict[str, Any]) -> str:
    """Conditional edge after synthesis_node — drives its two-phase self-loop.

    hitl_pending=True   → END (the operator cancelled the review, or a degraded
        suspend upstream. Checked FIRST and load-bearing for the same reason
        route_after_analyst checks it first: without it the self-loop below would
        spin until the recursion limit.)
    pending_brief set   → synthesis_node (the draft phase just committed a brief;
        revisit so the review phase can suspend on it as its first action)
    brief_revision_note → synthesis_node (the operator asked for a rewrite; revisit
        to re-distil under their steer)
    otherwise           → END (accepted; the parent graph's route_after_ideation
        reads ideation_synthesized and hands the brief to the planner)
    """
    if state.get("hitl_pending"):
        logger.info("route_after_synthesis: hitl_pending → END.")
        return END
    if state.get("pending_brief"):
        logger.info("route_after_synthesis: brief drafted → synthesis_node (review phase).")
        return "synthesis_node"
    if state.get("brief_revision_note"):
        logger.info("route_after_synthesis: rewrite requested → synthesis_node (re-draft).")
        return "synthesis_node"
    logger.info("route_after_synthesis: brief accepted → END (handoff to the planner).")
    return END


def _coverage_settled(state: Dict[str, Any]) -> bool:
    """True once the analyst reports no open dimensions left on this task.

    The axes are the model's own — it names what THIS task turns on, so a
    frontend change and a schema migration are not forced through the same set
    of angles. It closes the round with the sentinel below when every axis it
    named is resolved.

    Returns False whenever the signal is absent or unparseable — a model that
    ignored the format, a reasoning pass cut short by its token ceiling, a
    checkpoint written before the channel existed. That is the designed degrade
    path, not a failure: the caller then falls back to the round counter, which
    is the behaviour that shipped before coverage existed.
    """
    axes = state.get("grill_coverage_axes") or []
    if not isinstance(axes, list) or not axes:
        return False
    return all(str(a).strip().lower() in _SETTLED_AXES_SENTINELS for a in axes)


def route_after_analyst(state: Dict[str, Any]) -> str:
    """Conditional edge after analyst_grill.

    hitl_pending=True                  → END (degraded suspend — the analyst could
        not reach a model and already surfaced an actionable notice. Checked FIRST
        and load-bearing: without it the self-loop below would retry a dead model
        until the graph's recursion limit.)
    shared_understanding_reached=True  → synthesis_node (compress, hand off)
    coverage settled                   → synthesis_node (the analyst itself named
        the dimensions this task turns on, and now reports none left open — a
        criterion the task supplies rather than a fixed round budget spending
        rounds on a task that ran out of questions two rounds ago)
    otherwise                          → analyst_grill (loop for another batch of
        questions). The pause for the human's answer already happened inside
        the node via interrupt()/resume — this edge only decides whether
        another round is needed, never whether to suspend for an answer.

    Returns only values this edge's own path-map declares (brain/ideation.py's
    add_conditional_edges below); the coverage branch deliberately reuses
    synthesis_node rather than introducing a destination.
    """
    if state.get("hitl_pending"):
        logger.info("route_after_analyst: hitl_pending → END (degraded suspend).")
        return END
    if state.get("shared_understanding_reached"):
        logger.info("route_after_analyst: understanding reached → synthesis_node.")
        return "synthesis_node"
    if _coverage_settled(state):
        logger.info("route_after_analyst: coverage settled → synthesis_node.")
        return "synthesis_node"
    logger.info("route_after_analyst: another round needed → analyst_grill.")
    return "analyst_grill"


# ---------------------------------------------------------------------------
# Sub-graph construction
# ---------------------------------------------------------------------------
from agents.analyst import run_analyst_node  # noqa: E402 — deferred for engine.py compat

_NodeFn = TypeVar("_NodeFn", bound=Callable[..., Awaitable[Any]])


def _guarded(name: str, fn: _NodeFn) -> _NodeFn:
    """Reject an undeclared state key before it reaches LangGraph's silent filter.

    ``ideation_loop`` is added to the parent graph as a raw compiled subgraph
    (brain/engine.py), so its own nodes never pass through that module's
    ``_instrument_node`` wrapper — this is the local equivalent, scoped to
    ``assert_declared_channels`` only (no parent-graph telemetry to duplicate).
    This exact gap is what let ``ideation_synthesized`` go undeclared for the
    life of the feature: the handoff silently dropped on every run.

    Declares ``config`` explicitly, by name and annotation, for the same reason
    ``_instrument_node`` does: LangGraph inspects THIS callable to decide what to
    inject, and a variadic wrapper advertises no injectable parameter — which is
    what left the Socratic grill unable to narrate or stream its reasoning.

    Also the ideation subgraph's counterpart to `_instrument_node`'s agent-role/
    model-tier contextvar cleanup (`core/activity_context.py`) — without this,
    the grill and synthesis nodes would run under whatever the PARENT graph's
    node last bound, since this subgraph's nodes never pass through
    `_instrument_node` at all.
    """
    forwards_config = accepts_config(fn)
    derived_role = derive_node_role(name)

    async def _wrapped(
        state: Any, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Any:
        if forwards_config:
            kwargs["config"] = config
        role_token = bind_agent_role(derived_role)
        tier_token = bind_model_tier(None)
        try:
            result = await fn(state, **kwargs)
            assert_declared_channels(name, result)
            return result
        finally:
            reset_agent_role(role_token)
            reset_model_tier(tier_token)

    return cast(_NodeFn, _wrapped)


_ideation_workflow: StateGraph[AIlienantGraphState] = StateGraph(AIlienantGraphState)
_ideation_workflow.add_node("analyst_grill", _guarded("analyst_grill", run_analyst_node))  # type: ignore[type-var]
_ideation_workflow.add_node("synthesis_node", _guarded("synthesis_node", run_synthesis_node))  # type: ignore[type-var]
_ideation_workflow.add_edge(START, "analyst_grill")
_ideation_workflow.add_conditional_edges(
    "analyst_grill", route_after_analyst, ["analyst_grill", "synthesis_node", END]
)
_ideation_workflow.add_conditional_edges(
    "synthesis_node", route_after_synthesis, ["synthesis_node", END]
)

# No checkpointer — parent graph's CheckpointManager handles persistence.
ideation_graph = _ideation_workflow.compile()

logger.info("🟢 ideation_graph compiled: analyst_grill ⟲ (self-loop) → synthesis_node → END.")
