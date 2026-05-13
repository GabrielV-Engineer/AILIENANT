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

workflow.add_node("planner_agent", run_planner_node)
workflow.add_node("coder_agent", run_coder_node)

# =====================================================================
# 3. LÓGICA DE ENRUTAMIENTO (MapReduce Fan-Out)
# =====================================================================


def route_to_coders(state: AIlienantGraphState) -> list[Send]:
    """Conditional edge: fan-out desde PlannerAgent → N instancias de CoderAgent.

    High-TCI (>80): usa Send para lanzar una instancia de CoderAgent por cada
    WBSStep en parallel_tasks, ejecutándolos en paralelo dentro del grafo.

    Low/Medium-TCI: ejecuta un único CoderAgent secuencial con el primer
    paso pendiente del plan.
    """
    tci: float = state.get("tci", 0.0)
    parallel_tasks = state.get("parallel_tasks", [])

    if tci > 80.0 and parallel_tasks:
        logger.info(
            "🔀 High-TCI (%.1f): iniciando fan-out con %d CoderAgent(s) en paralelo.",
            tci,
            len(parallel_tasks),
        )
        return [
            Send("coder_agent", {**state, "current_step_id": step.step_number})
            for step in parallel_tasks
        ]

    # Fallback secuencial: primer paso pendiente del plan
    mission_spec = state.get("mission_spec")
    first_step = (
        next((t for t in mission_spec.tasks if t.status == "pending"), None)
        if mission_spec
        else None
    )
    logger.info(
        "➡️  Low/Medium-TCI (%.1f): ejecución secuencial, paso #%s.",
        tci,
        first_step.step_number if first_step else "None",
    )
    return [Send("coder_agent", {**state, "current_step_id": first_step.step_number if first_step else None})]


# =====================================================================
# 4. TOPOLOGÍA DEL GRAFO (Edges)
# =====================================================================
workflow.add_edge(START, "planner_agent")
workflow.add_conditional_edges("planner_agent", route_to_coders, ["coder_agent"])
workflow.add_edge("coder_agent", END)

# =====================================================================
# 5. COMPILACIÓN CON PERSISTENCIA (CheckpointManager)
# =====================================================================
# Usamos checkpoint_manager de brain/checkpoint.py para centralizar la
# gestión del ciclo de vida de la conexión SQLite. La instancia compilada
# `alienant_app` es importada por main.py y task_service.py.
with checkpoint_manager.get_saver() as _saver:
    alienant_app = workflow.compile(checkpointer=_saver)

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
