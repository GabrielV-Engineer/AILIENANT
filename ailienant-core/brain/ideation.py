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

import logging
from typing import List

from langgraph.graph import StateGraph, START, END

from brain.state import AIlienantGraphState, MissionSpecification

logger = logging.getLogger("IDEATION_GRAPH")

DEBUG_MODE = True  # Phase 4: real LLM synthesis


async def run_synthesis_node(state: dict) -> dict:
    """LangGraph node: compress the Socratic conversation into a MissionSpecification.

    Phase 4: real LLM call to extract ubiquitous_language, deep_modules_sdd,
    tdd_criteria from the accumulated messages.
    Phase 2.21: DEBUG stub — builds a placeholder MissionSpecification from metadata.
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
        raise NotImplementedError(
            "Phase 4: LLM synthesis of ubiquitous_language, deep_modules_sdd, "
            "tdd_criteria from Socratic messages."
        )

    logger.info(
        "SynthesisNode: MissionSpecification compressed from %d message(s). "
        "Handing off to autonomous execution (planner_mode_active=False).",
        len(messages),
    )

    return {
        "mission_spec": synthesis,
        "planner_mode_active": False,
        "shared_understanding_reached": True,
        "hitl_pending": False,
    }


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
