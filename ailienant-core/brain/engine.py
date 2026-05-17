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
from brain.drift_monitor import run_drift_monitor_node  # noqa: E402
from brain.finops import run_finops_node, route_after_finops  # noqa: E402
from brain.nodes.aggregator_node import run_session_delta_aggregator_node  # noqa: E402
from agents.contract_guard import run_contract_guard_node  # noqa: E402 — Phase 2.23


async def run_apply_patch_node(state: dict) -> dict:
    """Phase 2.2.D stub — applies pending_patches via blob_storage in Phase 4.

    In Phase 4: reads state["pending_patches"] (filepath → unified diff written by
    CoderAgent), calls blob_storage.apply_patch() per entry, and writes updated
    VFSFile(blob_hash=new_hash, ...) objects back into vfs_buffer.
    """
    return {}


from brain.ideation import ideation_graph  # noqa: E402 — deferred to avoid circular import


def route_after_summarize(state: dict) -> str:
    """Conditional edge: autonomous planner vs interactive ideation loop.

    planner_mode_active=True  → ideation_loop (Phase 2.21 interactive HITL)
    planner_mode_active=False → planner_agent (autonomous LLM planning)
    """
    from core.telemetry import log_routing_decision
    if state.get("planner_mode_active"):
        target = "ideation_loop"
        reason = "planner_mode_active=True"
    else:
        target = "planner_agent"
        reason = "planner_mode_active=False"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="summarize_history",
        target=target,
        reason=reason,
    )
    logger.info("route_after_summarize: planner_mode_active=%s → %s.", state.get("planner_mode_active"), target)
    return target


workflow.add_node("summarize_history", run_summarize_node)  # type: ignore[type-var]
workflow.add_node("planner_agent", run_planner_node)        # type: ignore[type-var]
workflow.add_node("drift_monitor", run_drift_monitor_node)  # type: ignore[type-var]
workflow.add_node("coder_agent", run_coder_node)            # type: ignore[type-var]
workflow.add_node("apply_patch", run_apply_patch_node)      # type: ignore[type-var]
workflow.add_node("validate_output", run_validate_output_node)  # type: ignore[type-var]
workflow.add_node("finops_gate", run_finops_node)           # type: ignore[type-var]
workflow.add_node("ideation_loop", ideation_graph)  # type: ignore[arg-type]
workflow.add_node("session_delta_aggregator", run_session_delta_aggregator_node)  # type: ignore[type-var]
workflow.add_node("contract_guard", run_contract_guard_node)  # type: ignore[type-var]  # Phase 2.23

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
    from core.telemetry import log_routing_decision
    provider: str = state.get("provider", "CLOUD")
    parallel_tasks = state.get("parallel_tasks", [])
    mission_spec = state.get("mission_spec")

    if provider == "CLOUD" and parallel_tasks:
        logger.info(
            "🔀 SWARM: provider=CLOUD, fan-out → %d CoderAgent(s) en paralelo.",
            len(parallel_tasks),
        )
        log_routing_decision(
            session_id=state.get("task_id", ""),
            source="drift_monitor",
            target="coder_agent",
            reason=f"SWARM: provider=CLOUD, {len(parallel_tasks)} tasks in parallel",
            css=state.get("css"),
            tci=state.get("tci"),
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
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="drift_monitor",
        target="coder_agent",
        reason=f"RELAY: provider={provider}, sequential execution",
        css=state.get("css"),
        tci=state.get("tci"),
    )
    return [Send("coder_agent", {**state, "current_step_id": first_pending.step_number if first_pending else None})]


# =====================================================================
# 4. TOPOLOGÍA DEL GRAFO (Edges)
# =====================================================================
workflow.add_edge(START, "summarize_history")
workflow.add_edge("summarize_history", "session_delta_aggregator")
workflow.add_conditional_edges(
    "session_delta_aggregator", route_after_summarize, ["planner_agent", "ideation_loop"]
)
workflow.add_edge("ideation_loop", END)
workflow.add_edge("planner_agent", "drift_monitor")
workflow.add_conditional_edges("drift_monitor", route_to_coders, ["coder_agent"])
# Phase 2.23 — ContractGuardNode is inserted as transparent middleware between
# CoderAgent and FinOpsGate. The node short-circuits internally (returns {} on
# quiet turns), so a routing callback would be cognitive noise. The node also
# owns contract_anchor mutation, which would have to be fragmented across the
# router otherwise — keeping it as a direct edge preserves a single ownership
# boundary for both the trigger evaluation and the anchor snapshot.
workflow.add_edge("coder_agent", "contract_guard")
workflow.add_edge("contract_guard", "finops_gate")
workflow.add_conditional_edges("finops_gate", route_after_finops, ["apply_patch", END])
workflow.add_edge("apply_patch", "validate_output")
workflow.add_conditional_edges("validate_output", route_after_validation, ["coder_agent", END])

# =====================================================================
# 5. COMPILACIÓN CON PERSISTENCIA (CheckpointManager)
# =====================================================================
# Usamos checkpoint_manager de brain/checkpoint.py para centralizar la
# gestión del ciclo de vida de la conexión SQLite. La instancia compilada
# `alienant_app` es importada por main.py y task_service.py.
alienant_app = workflow.compile(checkpointer=checkpoint_manager)

logger.info(
    "🟢 Motor AILIENANT compilado: "
    "SummarizeHistory → SessionDeltaAggregator → [PlannerAgent | IdeationLoop(Socratic)] → "
    "DriftMonitor → route_to_coders → CoderAgent(s) → ContractGuard → "
    "FinOpsGate → ApplyPatch → ValidateOutput."
)


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


# =====================================================================
# 7. TOP-LEVEL ROUTING ENTRY POINT (Phase 4.3)
# =====================================================================
# Re-exported from brain.intent_router so existing import sites
# (`from brain.engine import process_user_intent`) keep working unchanged.
# All three execution modes (SEQUENTIAL / MICRO_SWARM / FULL_SWARM) live there.
from brain.intent_router import process_user_intent  # noqa: E402,F401
