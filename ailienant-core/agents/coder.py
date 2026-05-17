# ailienant-core/agents/coder.py

import logging

from brain.state import WBSStep
# Phase 4.1.4 — role registry lives in agents/roles.py (flat-module import via conftest).
from roles import build_coder_system_prompt, get_role_config

logger = logging.getLogger("CODER_NODE")

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: set = set()


async def run_coder_node(state: dict) -> dict:
    """
    Nodo de LangGraph: El Ejecutor (CoderAgent stub — Phase 2 MapReduce).

    Recibe un slice de estado con `current_step_id` inyectado por `Send` desde
    el fan-out del Planner. Marca el paso como in_progress, reserva su slot en
    vfs_buffer, y retorna el delta de estado para que el reducer _merge_vfs lo
    fusione de forma segura con los demás hilos concurrentes.

    El cuerpo real de generación de código (LLM call + RBWE) se implementa en Phase 4.
    """
    # If the guardrail flagged invalid output on a previous attempt, inject the
    # corrective system message so the LLM knows what to fix (Phase 2.1.14).
    validation_feedback = state.get("validation_feedback")
    if validation_feedback:
        logger.info(
            "CoderAgent: retrying with guardrail feedback (retry %d)",
            state.get("retry_count", 0),
        )

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

    # Phase 4.1.4 — Cognitive Policy Engine: resolve role config + build ephemeral
    # system prompt. CRITICAL invariant: ephemeral_system_prompt is a LOCAL VAR only.
    # It is NEVER written to state.messages or returned in the result dict. Phase 5's
    # MCP executor re-resolves the role config and prompt at runtime via the
    # module-level singleton ROLE_REGISTRY (O(1) lookup, no perf penalty).
    role_cfg = get_role_config(target_step.target_role)
    ephemeral_system_prompt: str = build_coder_system_prompt(target_step.target_role)
    logger.debug(
        "CoderAgent: built ephemeral prompt for role=%s (%d chars).",
        target_step.target_role,
        len(ephemeral_system_prompt),
    )

    # Pre-execution HITL gates — emit security flags when the active step matches a
    # role-specific HITL trigger (e.g. devops_infra touching .env, vcs_manager --force).
    new_security_flags: list[str] = []
    task_blob = f"{target_step.target_file} {target_step.description}"
    for trigger in role_cfg["hitl_triggers"]:
        if trigger in task_blob:
            flag = f"HITL_APPROVAL_REQUIRED:{target_step.target_role}:{trigger}"
            new_security_flags.append(flag)
            logger.warning(
                "CoderAgent: HITL trigger '%s' matched on step #%d (role=%s).",
                trigger,
                target_step.step_number,
                target_step.target_role,
            )

    # Marcamos el paso como en progreso (Phase 4 lo mutará a 'completed' o 'failed').
    target_step.status = "in_progress"

    # Emit step mutation to the frontend — non-blocking (graph must not wait on WS).
    # Deferred import mirrors drift_monitor.py / finops.py pattern (avoids circular import).
    import asyncio
    from api.websocket_manager import vfs_manager
    _t = asyncio.create_task(
        vfs_manager.emit_graph_mutation(
            session_id=state.get("task_id", ""),
            step_number=target_step.step_number,
            new_status="in_progress",
            agent_name="CoderAgent",
        )
    )
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)

    # Reservamos el slot en vfs_buffer. El reducer _merge_vfs fusionará esto
    # con los resultados de los demás CoderAgent que corran en paralelo.
    # Phase 4.1.4 — STATE-KEY CONTRACT (R1): every key here must exist in
    # AIlienantGraphState (brain/state.py:206-308). Returning a non-state key
    # like `allowed_tools` would break LangGraph's state-merge contract or bloat
    # the SQLite checkpoint. The role config is re-resolved by Phase 5 MCP.
    result: dict = {
        "vfs_buffer": {},   # Phase 4 escribirá VFSFile objetos aquí
        "target_role": target_step.target_role,
        "current_step_id": target_step.step_number,
        "current_cost_usd": 0.0,   # Phase 4 returns real token cost delta
    }
    if new_security_flags:
        result["security_flags"] = new_security_flags
    return result
