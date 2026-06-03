# ailienant-core/brain/ideation.py
#
# Phase 2.21 — Socratic Ideation Sub-Graph.
#
# Compiled without a checkpointer — the parent graph's CheckpointManager
# (brain/checkpoint.py / brain/engine.py) handles all persistence.
#
# Node topology:
#   analyst_grill → [route_after_analyst]
#       shared_understanding_reached=True  → synthesis_node → END
#       shared_understanding_reached=False → END  (suspend; await next task_service call)

import json
import logging
import os as _os
from typing import Any, Dict, List

from langgraph.graph import StateGraph, START, END

from brain.state import AIlienantGraphState, MissionSpecification

logger = logging.getLogger("IDEATION_GRAPH")

# Live structured synthesis is the default; the placeholder stub is the
# deterministic escape hatch for CI/UI smoke tests. Mirrors planner/analyst:
# set AILIENANT_IDEATION_DEBUG=1 to force the stub.
DEBUG_MODE: bool = _os.getenv("AILIENANT_IDEATION_DEBUG", "0") != "0"

# Compress the accumulated Socratic dialogue into a strict MissionSpecification.
# The model must emit the WBS too — an empty task list is what made the planner
# announce "no concrete edits", so we demand concrete, buildable steps.
_SYNTHESIS_SYSTEM_PROMPT: str = (
    "You are the AnalystAgent synthesizing a finished plan from a completed "
    "Socratic planning dialogue. Compress the whole conversation into a single "
    "strict JSON object matching this schema (no prose, no markdown fences):\n"
    '{\n'
    '  "outcome": "<one-paragraph statement of the end result and its value>",\n'
    '  "scope": ["<files/areas IN scope>", "<and explicitly OUT of scope>"],\n'
    '  "constraints": ["<technical limits agreed during the dialogue>"],\n'
    '  "decisions": ["<design/architecture decisions taken>"],\n'
    '  "tasks": [\n'
    '    {"step_number": 1, "target_role": "core_dev", '
    '"action": "edit_file", "target_file": "<path>", '
    '"description": "<precise instruction for this step>"}\n'
    '  ],\n'
    '  "checks": ["<acceptance criteria — how we know it works>"],\n'
    '  "ubiquitous_language": {"<term>": "<definition>"},\n'
    '  "tdd_criteria": ["<test-first acceptance criteria>"]\n'
    '}\n'
    "RULES:\n"
    "- target_role is one of: core_dev, architect_refactor, devops_infra, "
    "secops, qa_tester, doc_manager, vcs_manager, data_ml_engineer.\n"
    "- action is one of: read_file, write_file, edit_file, run_command.\n"
    "- tasks MUST be concrete and buildable, ordered by step_number starting at "
    "1, each naming a real target_file derived from the dialogue and workspace. "
    "Never return an empty tasks list when the dialogue described work to do.\n"
    "- Mirror the language of the dialogue in all prose fields."
)


def _dialogue_transcript(messages: List[dict]) -> str:
    """Flatten the accumulated Q&A into a plain transcript for the synthesis prompt."""
    lines: List[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            speaker = "USER" if role == "user" else "ANALYST"
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _fallback_mission(messages: List[dict]) -> MissionSpecification:
    """Honest minimal plan when structured synthesis cannot produce a WBS.

    Carries an empty ``tasks`` list so ``_format_coding_summary`` surfaces the
    truthful "no concrete edits" line instead of the graph crashing — the user
    can then refine the dialogue rather than hitting an opaque failure.
    """
    return MissionSpecification(
        outcome=(
            f"Plan synthesized from the Socratic dialogue "
            f"({len(messages)} message(s)), but no concrete file steps could be "
            f"extracted — refine the plan and try again."
        ),
        scope=[],
        constraints=[],
        decisions=[],
        tasks=[],
        checks=[],
    )


async def run_synthesis_node(state: dict) -> dict:
    """LangGraph node: compress the Socratic conversation into a MissionSpecification.

    Live path: one structured LLM call extracts the full SDD contract — outcome,
    scope, constraints, decisions, a concrete WBS, acceptance checks, and the DDD
    optionals — from the accumulated dialogue. On a parse/validation failure it
    degrades to an honest empty-WBS plan rather than crashing the graph.
    """
    messages: List[dict] = state.get("messages", [])

    if DEBUG_MODE:
        synthesis = MissionSpecification(
            outcome=(
                f"Plan synthesized from Socratic dialogue "
                f"({len(messages)} message(s) accumulated)."
            ),
            scope=["Derived from Socratic Q&A session."],
            constraints=["Constraints identified during ideation."],
            decisions=["Decisions agreed upon during dialogue."],
            tasks=[],  # PlannerAgent populates full WBS in autonomous mode
            checks=["TDD criteria agreed in the Socratic session."],
            ubiquitous_language={},
            deep_modules_sdd=None,
            tdd_criteria=[],
        )
    else:
        synthesis = await _synthesize_mission_llm(state, messages)

    logger.info(
        "SynthesisNode: MissionSpecification compressed from %d message(s) "
        "(%d WBS step(s)). Handing off to autonomous execution.",
        len(messages), len(synthesis.tasks),
    )

    return {
        "mission_spec": synthesis,
        "planner_mode_active": False,
        "shared_understanding_reached": True,
        "hitl_pending": False,
    }


async def _synthesize_mission_llm(
    state: dict, messages: List[dict]
) -> MissionSpecification:
    """Structured LLM synthesis of the dialogue into a MissionSpecification.

    Grounds the WBS in the workspace (active file + GraphRAG, best-effort) and
    validates the model's JSON by constructing the Pydantic model — its
    before-validators coerce hallucinated role names and scalar-vs-list fields, so
    a slightly-off payload still yields a valid plan. Any failure falls back to an
    honest empty-WBS plan; this node never raises.
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
                {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MODEL_BIG,
            temperature=0.0,
            response_format={"type": "json_object"},
            session_id=session_id,
            state=state,
        )
        raw = LLMGateway._sanitize_json_response(resp.choices[0].message.content or "")
        parsed: Dict[str, Any] = json.loads(raw)
        return MissionSpecification(**parsed)
    except Exception as exc:  # noqa: BLE001 — synthesis must never crash the graph
        logger.warning("SynthesisNode: structured synthesis failed (%s: %s); "
                       "falling back to honest empty-WBS plan.",
                       type(exc).__name__, exc)
        return _fallback_mission(messages)


async def _assemble_synthesis_context(state: dict) -> str:
    """Best-effort workspace context block to ground the WBS in real file paths."""
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


def route_after_analyst(state: dict) -> str:
    """Conditional edge after analyst_grill.

    shared_understanding_reached=True  → synthesis_node (compress, hand off)
    shared_understanding_reached=False → END (suspend, await next user turn)
    """
    if state.get("shared_understanding_reached"):
        logger.info("route_after_analyst: understanding reached → synthesis_node.")
        return "synthesis_node"
    logger.info("route_after_analyst: hitl_pending=True → END (awaiting user response).")
    return END


# ---------------------------------------------------------------------------
# Sub-graph construction
# ---------------------------------------------------------------------------
from agents.analyst import run_analyst_node  # noqa: E402 — deferred for engine.py compat

_ideation_workflow: StateGraph = StateGraph(AIlienantGraphState)
_ideation_workflow.add_node("analyst_grill", run_analyst_node)  # type: ignore[type-var]
_ideation_workflow.add_node("synthesis_node", run_synthesis_node)  # type: ignore[type-var]
_ideation_workflow.add_edge(START, "analyst_grill")
_ideation_workflow.add_conditional_edges(
    "analyst_grill", route_after_analyst, ["synthesis_node", END]
)
_ideation_workflow.add_edge("synthesis_node", END)

# No checkpointer — parent graph's CheckpointManager handles persistence.
ideation_graph = _ideation_workflow.compile()

logger.info("🟢 ideation_graph compiled: analyst_grill → [synthesis_node | END].")
