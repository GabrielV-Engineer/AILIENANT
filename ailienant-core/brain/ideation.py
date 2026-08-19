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

from brain.state import AIlienantGraphState, assert_declared_channels

logger = logging.getLogger("IDEATION_GRAPH")

# Live distillation is the default; the placeholder stub is the deterministic
# escape hatch for CI/UI smoke tests. Mirrors planner/analyst:
# set AILIENANT_IDEATION_DEBUG=1 to force the stub.
DEBUG_MODE: bool = _os.getenv("AILIENANT_IDEATION_DEBUG", "0") != "0"

# Distill the Socratic dialogue into a SOFT brief — intent + hard constraints +
# domain glossary. Deliberately NOT the rigid MissionSpecification: a missing field
# degrades gracefully, so there is no schema gamble here. The downstream PlannerAgent
# turns this brief into the validated WBS under its reflection loop.
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


async def run_synthesis_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """LangGraph node: distill the Socratic dialogue, then hand off to the planner.

    This node does not produce a plan. It compresses the conversation into a soft
    brief, folds it into ``user_input``, and sets ``ideation_synthesized`` so the
    parent graph routes the turn into the autonomous PlannerAgent — whose
    draft→validate→re-draft loop produces the schema-valid WBS. ``mission_spec`` is
    intentionally left for the planner to own.
    """
    messages: List[Dict[str, Any]] = state.get("messages", [])
    _narrate = (config or {}).get("configurable", {}).get("narrate")

    async def _emit(node_name: str) -> None:
        if _narrate is not None:
            await _narrate(node_name)

    await _emit("synthesizing_intent")

    fallback_intent = _dialogue_transcript(messages) or (state.get("user_input") or "")
    if DEBUG_MODE:
        planner_brief = fallback_intent
        glossary: Dict[str, str] = {}
    else:
        brief = await _distill_brief_llm(state, messages)
        planner_brief = _compose_planner_brief(brief, fallback_intent)
        _gloss = brief.get("ubiquitous_language") or {}
        glossary = {str(k): str(v) for k, v in _gloss.items()} if isinstance(_gloss, dict) else {}

    logger.info(
        "SynthesisNode: distilled brief from %d message(s) (%d char(s)). "
        "Handing off to the autonomous PlannerAgent.",
        len(messages), len(planner_brief),
    )

    await _emit("handoff_to_planner")

    return {
        "user_input": planner_brief,
        "ideation_glossary": glossary,
        "ideation_synthesized": True,
        "planner_mode_active": False,
        "shared_understanding_reached": True,
        "hitl_pending": False,
    }


async def _distill_brief_llm(state: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Soft-schema distillation of the dialogue into an intent/constraints brief.

    Grounds the brief in the workspace (active file + GraphRAG, best-effort). Never
    raises — a parse failure degrades to an intent-only brief so the handoff to the
    planner always proceeds (the planner's reflection loop carries the rigor).
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from shared.config import MODEL_BIG

    transcript = _dialogue_transcript(messages)
    context_block = await _assemble_synthesis_context(state)
    user_payload = transcript
    if context_block:
        user_payload = f"{transcript}\n\n### Workspace context\n{context_block}"

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
    """Best-effort workspace context block to ground the distilled brief."""
    active_path: str = state.get("active_file_path") or ""
    paths: List[str] = [active_path] if active_path else []
    project_root: str = state.get("workspace_root") or ""
    if not paths and not project_root:
        return ""
    try:
        from agents.analyst_context import assemble_analyst_context
        return await assemble_analyst_context(
            paths, state.get("project_id") or None, state.get("task_id", ""),
            project_root=project_root,
        )
    except Exception as exc:  # noqa: BLE001 — context is best-effort
        logger.debug("Synthesis context assembly failed (degrading): %s", exc)
        return ""


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
    """

    async def _wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
        result = await fn(state, *args, **kwargs)
        assert_declared_channels(name, result)
        return result

    return cast(_NodeFn, _wrapped)


_ideation_workflow: StateGraph[AIlienantGraphState] = StateGraph(AIlienantGraphState)
_ideation_workflow.add_node("analyst_grill", _guarded("analyst_grill", run_analyst_node))  # type: ignore[type-var]
_ideation_workflow.add_node("synthesis_node", _guarded("synthesis_node", run_synthesis_node))  # type: ignore[type-var]
_ideation_workflow.add_edge(START, "analyst_grill")
_ideation_workflow.add_conditional_edges(
    "analyst_grill", route_after_analyst, ["analyst_grill", "synthesis_node", END]
)
_ideation_workflow.add_edge("synthesis_node", END)

# No checkpointer — parent graph's CheckpointManager handles persistence.
ideation_graph = _ideation_workflow.compile()

logger.info("🟢 ideation_graph compiled: analyst_grill ⟲ (self-loop) → synthesis_node → END.")
