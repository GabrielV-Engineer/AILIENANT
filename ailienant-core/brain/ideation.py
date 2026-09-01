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
# that function's own rationale). Derived from the alias against the
# resolver's tier vocabulary, mirroring `agents/analyst.py::_GRILL_TIER`,
# rather than restating "big" as a second literal (§5.7).
from core.config.model_resolver import _TIER_ORDER  # noqa: E402
from shared.config import MODEL_BIG as _SYNTHESIS_MODEL  # noqa: E402
_SYNTHESIS_TIER: str = next(
    (t for t in _TIER_ORDER if _SYNTHESIS_MODEL == f"ailienant/{t}"), "big"
)

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

_DISTILL_SYSTEM_PROMPT: str = (
    "You are the AnalystAgent closing a Socratic planning dialogue. Distill the "
    "whole conversation into a concise build brief for an autonomous planner — NOT "
    "a full plan. Return a single JSON object (no prose, no markdown fences):\n"
    '{\n'
    '  "intent": "<one tight paragraph: what to build and what done looks like>",\n'
    '  "constraints": ["<hard technical limits agreed in the dialogue>"],\n'
    '  "scope_hints": ["<files/areas in or out of scope, if named>"],\n'
    '  "ubiquitous_language": {"<term>": "<definition>"}\n'
    '}\n'
    "Capture only what the dialogue actually settled; do not invent a work "
    "breakdown, file edits, or steps — the planner does that. Mirror the language "
    "of the dialogue."
)


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


def _compose_planner_brief(brief: Dict[str, Any], fallback: str) -> str:
    """Render the distilled brief into the prose ``user_input`` the planner reads.

    The planner consumes ``user_input`` as its requirement statement; folding the
    settled intent + constraints + glossary into it lets the planner draft a WBS
    grounded in the Socratic outcome without re-litigating the dialogue.
    """
    intent = str(brief.get("intent") or "").strip() or fallback
    parts: List[str] = [intent]
    constraints = [str(c) for c in (brief.get("constraints") or []) if str(c).strip()]
    if constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in constraints))
    hints = [str(h) for h in (brief.get("scope_hints") or []) if str(h).strip()]
    if hints:
        parts.append("Scope:\n" + "\n".join(f"- {h}" for h in hints))
    glossary = brief.get("ubiquitous_language") or {}
    if isinstance(glossary, dict) and glossary:
        gloss = "; ".join(f"{k} = {v}" for k, v in glossary.items())
        parts.append(f"Glossary: {gloss}")
    return "\n\n".join(parts)


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
    if DEBUG_MODE:
        brief: Dict[str, Any] = {"intent": fallback_intent}
        planner_brief = fallback_intent
    else:
        brief = await _distill_brief_llm(state, messages, revision_note=revision_note)
        planner_brief = _compose_planner_brief(brief, fallback_intent)

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
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from shared.config import MODEL_BIG

    transcript = _dialogue_transcript(messages)
    context_block = await _assemble_synthesis_context(state)
    user_payload = transcript
    if context_block:
        user_payload = f"{transcript}\n\n### Workspace context\n{context_block}"
    if revision_note:
        user_payload = (
            f"{user_payload}\n\n### Correction to the previous brief\n"
            f"The operator read your previous brief and asked for this: {revision_note}\n"
            "Re-distil the same dialogue accordingly. Do not invent anything the "
            "dialogue did not settle."
        )

    session_id: str = state.get("task_id", "")
    try:
        resp = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MODEL_BIG,
            temperature=0.0,
            response_format={"type": "json_object"},
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


def route_after_analyst(state: Dict[str, Any]) -> str:
    """Conditional edge after analyst_grill.

    hitl_pending=True                  → END (degraded suspend — the analyst could
        not reach a model and already surfaced an actionable notice. Checked FIRST
        and load-bearing: without it the self-loop below would retry a dead model
        until the graph's recursion limit.)
    shared_understanding_reached=True  → synthesis_node (compress, hand off)
    otherwise                          → analyst_grill (loop for another batch of
        questions). The pause for the human's answer already happened inside
        the node via interrupt()/resume — this edge only decides whether
        another round is needed, never whether to suspend for an answer.
    """
    if state.get("hitl_pending"):
        logger.info("route_after_analyst: hitl_pending → END (degraded suspend).")
        return END
    if state.get("shared_understanding_reached"):
        logger.info("route_after_analyst: understanding reached → synthesis_node.")
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
