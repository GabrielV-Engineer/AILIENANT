# ailienant-core/brain/engine.py

import logging
from typing import Callable, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

from brain.state import AIlienantGraphState
from brain.checkpoint import checkpoint_manager

logger = logging.getLogger("AILIENANT_ENGINE")

# =====================================================================
# 1. INICIALIZACIÓN DEL GRAFO
# =====================================================================
workflow = StateGraph(AIlienantGraphState)

# =====================================================================
# 2. NODOS DEL GRAFO (Phase 2)
# =====================================================================
# Importaciones diferidas para evitar dependencias circulares en el startup.
from agents.planner import run_planner_node  # noqa: E402
from agents.coder import run_coder_node      # noqa: E402
from brain.summarizer import run_summarize_node  # noqa: E402
from brain.guardrails import run_validate_output_node, route_after_validation  # noqa: E402

workflow.add_node("summarize_history", run_summarize_node)  # type: ignore[type-var]
workflow.add_node("planner_agent", run_planner_node)        # type: ignore[type-var]
workflow.add_node("coder_agent", run_coder_node)            # type: ignore[type-var]
workflow.add_node("validate_output", run_validate_output_node)  # type: ignore[type-var]

# =====================================================================
# 3. LÓGICA DE ENRUTAMIENTO (MapReduce Fan-Out)
# =====================================================================


def route_to_coders(state: AIlienantGraphState) -> list[Send]:
    """Conditional edge implementing two execution topologies:

    SWARM (provider == "CLOUD"):
      MapReduce fan-out — one CoderAgent instance per task in parallel_tasks.
      All instances run concurrently; _merge_generated_code resolves collisions.

    RELAY (provider == "LOCAL" or fallback):
      Relay State Machine — sends only the next pending task to a single CoderAgent.
      Sequential execution protects VRAM from concurrent inference pressure.
      After the CoderAgent marks its step 'completed', the next graph invocation
      advances the pointer to the following pending step.
    """
    provider: str = state.get("provider", "CLOUD")
    parallel_tasks = state.get("parallel_tasks", [])
    mission_spec = state.get("mission_spec")

    if provider == "CLOUD" and parallel_tasks:
        logger.info(
            "🔀 SWARM: provider=CLOUD, fan-out → %d CoderAgent(s) en paralelo.",
            len(parallel_tasks),
        )
        return [
            Send("coder_agent", {**state, "current_step_id": step.step_number})
            for step in parallel_tasks
        ]

    # RELAY: send exactly one pending step to protect VRAM
    first_pending = (
        next((t for t in mission_spec.tasks if t.status == "pending"), None)
        if mission_spec
        else None
    )
    logger.info(
        "➡️  RELAY: provider=%s, ejecución secuencial → paso #%s.",
        provider,
        first_pending.step_number if first_pending else "None",
    )
    return [Send("coder_agent", {**state, "current_step_id": first_pending.step_number if first_pending else None})]


# =====================================================================
# 4. TOPOLOGÍA DEL GRAFO (Edges)
# =====================================================================
workflow.add_edge(START, "summarize_history")
workflow.add_edge("summarize_history", "planner_agent")
workflow.add_conditional_edges("planner_agent", route_to_coders, ["coder_agent"])
workflow.add_edge("coder_agent", "validate_output")
workflow.add_conditional_edges("validate_output", route_after_validation, ["coder_agent", END])

# =====================================================================
# 5. COMPILACIÓN CON PERSISTENCIA (CheckpointManager)
# =====================================================================
# Usamos checkpoint_manager de brain/checkpoint.py para centralizar la
# gestión del ciclo de vida de la conexión SQLite. La instancia compilada
# `alienant_app` es importada por main.py y task_service.py.
alienant_app = workflow.compile(checkpointer=checkpoint_manager)

logger.info("🟢 Motor AILIENANT compilado: PlannerAgent → route_to_coders → CoderAgent(s).")


# =====================================================================
# 6. CONTEXT ASSEMBLY UTILITIES (Phase 1.1.0.4)
# =====================================================================


def resolve_explicit_mentions(
    explicit_mentions: list[str],
    vfs_read: Callable[[str], Optional[str]],
    boundary: str,
) -> str:
    """Read full content for @-mentioned files, bypassing the GraphRAG threshold.

    Takes a callable so this function stays decoupled from VFSMiddleware and is
    unit-testable with a simple lambda. Callers pass `vfs_instance.read`.

    Logs 'RAG bypass: full-file injection → <path>' for each successful read so
    the DoD log check can be verified without a running graph.
    """
    parts: list[str] = []
    for path in explicit_mentions:
        content = vfs_read(path)
        if content is not None:
            logger.info("RAG bypass: full-file injection → %s", path)
            parts.append(
                f'<{boundary} filepath="{path}" source="explicit_mention">\n{content}\n</{boundary}>'
            )
        else:
            logger.warning("explicit_mention not found in VFS or disk: %s", path)
    return "\n\n".join(parts)
