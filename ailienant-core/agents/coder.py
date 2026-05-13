# ailienant-core/agents/coder.py

import logging

from brain.state import WBSStep

logger = logging.getLogger("CODER_NODE")


async def run_coder_node(state: dict) -> dict:
    """
    Nodo de LangGraph: El Ejecutor (CoderAgent stub — Phase 2 MapReduce).

    Recibe un slice de estado con `current_step_id` inyectado por `Send` desde
    el fan-out del Planner. Marca el paso como in_progress, reserva su slot en
    vfs_buffer, y retorna el delta de estado para que el reducer _merge_vfs lo
    fusione de forma segura con los demás hilos concurrentes.

    El cuerpo real de generación de código (LLM call + RBWE) se implementa en Phase 4.
    """
    step_id: int | None = state.get("current_step_id")
    mission_spec = state.get("mission_spec")

    if mission_spec is None:
        logger.error("CoderAgent invocado sin mission_spec en el estado.")
        return {"errors": ["CoderAgent: mission_spec ausente — abortando paso."]}

    target_step: WBSStep | None = next(
        (t for t in mission_spec.tasks if t.step_number == step_id),
        None,
    )

    if target_step is None:
        logger.error("CoderAgent: step_id=%s no encontrado en mission_spec.", step_id)
        return {"errors": [f"CoderAgent: WBSStep #{step_id} no existe en el plan."]}

    logger.info(
        "⚙️  CoderAgent ejecutando paso #%d [%s] → %s",
        target_step.step_number,
        target_step.target_role,
        target_step.target_file,
    )

    # Marcamos el paso como en progreso (Phase 4 lo mutará a 'completed' o 'failed').
    target_step.status = "in_progress"

    # Reservamos el slot en vfs_buffer. El reducer _merge_vfs fusionará esto
    # con los resultados de los demás CoderAgent que corran en paralelo.
    return {
        "vfs_buffer": {},   # Phase 4 escribirá VFSFile objetos aquí
        "target_role": target_step.target_role,
        "current_step_id": target_step.step_number,
    }
