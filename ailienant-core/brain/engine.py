# ailienant-core/brain/engine.py

import asyncio
import functools
import logging
import traceback as _tb
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar, cast

from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

from brain.state import AIlienantGraphState
from brain.checkpoint import checkpoint_manager
from brain.failure_breaker import failure_breaker, normalize_signature
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS
from core.dead_letter import dead_letter_decorator  # Phase 6.4 — DLQ node wrap
from core.telemetry_log import log_node_transition

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
from core.supervisor import run_supervisor_node, route_after_supervisor  # noqa: E402 — Phase 6.5
from agents.error_correction import run_error_correction_node  # noqa: E402 — self-healing reflexion node
# Autonomous ReAct execution cell. engine.py imports the node only — the MCTS edge it uses
# for branch governance lives entirely inside brain.agentic_cell, so the live graph spine
# never imports the offline tree directly.
from brain.agentic_cell import run_agentic_cell_node, route_after_cell  # noqa: E402


async def run_apply_patch_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """applies pending_patches via blob_storage.

    reads state["pending_patches"] (filepath → unified diff written by
    CoderAgent), calls blob_storage.apply_patch() per entry, and writes updated
    VFSFile(blob_hash=new_hash, ...) objects back into vfs_buffer.
    """
    return {}


from brain.ideation import ideation_graph  # noqa: E402 — deferred to avoid circular import


def route_after_summarize(state: Dict[str, Any]) -> str:
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


def route_after_ideation(state: Dict[str, Any]) -> str:
    """Conditional edge after the Socratic ideation sub-graph.

    The ideation loop either suspended mid-dialogue (analyst still grilling) or
    distilled the conversation into a planner brief. On synthesis we hand the brief
    to the autonomous PlannerAgent — its Actor-Critic reflection loop produces the
    schema-valid WBS — so the Socratic outcome never dead-ends at a zero-shot plan.

    hitl_pending=True        → END (suspend; the next user turn resumes the dialogue)
    ideation_synthesized=True → planner_agent (run the reflection loop on the brief)
    """
    from core.telemetry import log_routing_decision
    if state.get("hitl_pending"):
        target = END
        reason = "ideation_suspended_awaiting_user"
    elif state.get("ideation_synthesized"):
        target = "planner_agent"
        reason = "ideation_synthesized_handoff"
    else:
        target = END  # defensive: nothing distilled and not suspended → nothing to plan
        reason = "ideation_no_op"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="ideation_loop",
        target=str(target),
        reason=reason,
    )
    logger.info("route_after_ideation: → %s (%s).", target, reason)
    return target


_NodeFn = TypeVar("_NodeFn", bound=Callable[..., Awaitable[Any]])


def _instrument_node(name: str, fn: _NodeFn) -> _NodeFn:
    """Mirror every graph node entry to the live telemetry sink.

    The deterministic edges and the externally-defined conditional routers do not
    pass through ``log_routing_decision``; wrapping the node entrypoints here makes
    each transition visible in ``.ailienant_telemetry.log`` without coupling the
    routers to the sink. Best-effort and off-loop — a sink failure never blocks the
    node, and the enqueue is O(1). The original callable type is preserved so the
    ``add_node`` overloads still resolve.
    """
    async def _wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            session_id = str(state.get("task_id", "")) if isinstance(state, dict) else ""
            log_node_transition(session_id=session_id, source="graph", target=name, reason="node_enter")
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            pass
        # Forward the runtime-supplied RunnableConfig (and any positional extras)
        # so nodes that declare a `config` parameter receive it — LangGraph inspects
        # the outermost callable's signature, so the wrapper must be variadic.
        return await fn(state, *args, **kwargs)

    return cast(_NodeFn, _wrapped)


_REFLEXION_TRACE_CAP: int = 4000


def reflexion_guard(node_name: str) -> Callable[[_NodeFn], _NodeFn]:
    """Trap a node exception into a self-healing signal instead of letting it die.

    Composes INSIDE ``dead_letter_decorator``: on a fresh, in-budget failure whose
    signature the cross-turn breaker still permits, the exception is swallowed and a
    ``healing_required`` delta is returned so a conditional edge can route to the
    ErrorCorrectionAgent. Once the in-turn budget is spent OR the signature breaker is
    OPEN, the exception is re-raised so the outer DLQ decorator records the episode and
    the turn concedes gracefully (recoverable from the Recovery surface).

    ``asyncio.CancelledError`` is always re-raised — user-abort / cascade-cancel must
    never be mistaken for a healable fault.
    """

    def decorator(fn: _NodeFn) -> _NodeFn:
        @functools.wraps(fn)
        async def _wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                # Variadic passthrough so the runtime RunnableConfig reaches a node
                # that declares `config` — LangGraph reads the outermost signature.
                return await fn(state, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — convert to a healing signal or concede
                attempts = (
                    int(state.get("correction_attempts", 0)) if isinstance(state, dict) else 0
                )
                signature = normalize_signature(node_name, type(exc).__name__, str(exc))
                if attempts >= CORRECTION_MAX_ATTEMPTS or not failure_breaker.allow(signature):
                    raise  # concede to the DLQ via the outer dead_letter_decorator
                tb_text = "".join(
                    _tb.format_exception(type(exc), exc, exc.__traceback__)
                )[:_REFLEXION_TRACE_CAP]
                logger.warning(
                    "reflexion_guard: trapping %s failure (attempt %d/%d): %s",
                    node_name, attempts + 1, CORRECTION_MAX_ATTEMPTS, exc,
                )
                return {
                    "healing_required": True,
                    "correction_attempts": attempts + 1,
                    "last_error_trace": tb_text,
                    "failed_node": node_name,
                    "failure_signature": signature,
                }

        return cast(_NodeFn, _wrapped)

    return decorator


def route_after_coder(state: Dict[str, Any]) -> str:
    """Conditional edge: divert to self-healing when the reflexion guard tripped,
    else proceed to the contract guard on the normal path."""
    from core.telemetry import log_routing_decision
    healing = bool(state.get("healing_required"))
    target = "error_correction" if healing else "contract_guard"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="coder_agent",
        target=target,
        reason="healing_required" if healing else "coder_ok",
    )
    return target


workflow.add_node("summarize_history", _instrument_node("summarize_history", run_summarize_node))  # type: ignore[type-var]
# DLQ-wrapped node entrypoints. An unhandled exception promotes
# L1→L2 and persists a dead_letter_tasks row before re-raising (see
# core/dead_letter.py). The 4 wrapped nodes are the state-bearing entrypoints;
# summarize_history / drift_monitor / contract_guard / finops_gate are left bare.
# The decorator's Callable[...] return satisfies add_node without the type-var
# suppression the bare node functions still need.
workflow.add_node("planner_agent", _instrument_node("planner_agent", dead_letter_decorator("planner_agent")(run_planner_node)))
workflow.add_node("drift_monitor", _instrument_node("drift_monitor", run_drift_monitor_node))
# coder_agent is also wrapped by reflexion_guard (INSIDE the DLQ decorator): a fresh,
# in-budget failure becomes a healing signal routed to error_correction; an exhausted
# budget re-raises into the DLQ.
workflow.add_node("coder_agent", _instrument_node("coder_agent", dead_letter_decorator("coder_agent")(reflexion_guard("coder_agent")(run_coder_node))))
workflow.add_node("error_correction", _instrument_node("error_correction", dead_letter_decorator("error_correction")(run_error_correction_node)))
workflow.add_node("apply_patch", _instrument_node("apply_patch", dead_letter_decorator("apply_patch")(run_apply_patch_node)))
workflow.add_node("validate_output", _instrument_node("validate_output", dead_letter_decorator("validate_output")(run_validate_output_node)))
workflow.add_node("finops_gate", _instrument_node("finops_gate", run_finops_node))
workflow.add_node("ideation_loop", ideation_graph)
workflow.add_node("session_delta_aggregator", _instrument_node("session_delta_aggregator", run_session_delta_aggregator_node))
workflow.add_node("contract_guard", _instrument_node("contract_guard", run_contract_guard_node))  # Phase 2.23
# deterministic FinOps Supervisor spliced between finops_gate and
# apply_patch. DLQ-wrapped an AuditChainBrokenError becomes a
# recoverable dead_letter_tasks episode rather than a silent graph death.
workflow.add_node(
    "supervisor_node",
    _instrument_node("supervisor_node", dead_letter_decorator("supervisor_node")(run_supervisor_node)),
)
# Autonomous ReAct cell — same wrapper stack as coder_agent (DLQ + instrumentation).
# A non-converging loop concedes gracefully inside the node, so it does not need the
# reflexion guard; an unexpected fault still promotes to the DLQ.
workflow.add_node(
    "agentic_cell",
    _instrument_node("agentic_cell", dead_letter_decorator("agentic_cell")(run_agentic_cell_node)),
)

# =====================================================================
# 3. ROUTING LOGIC (MapReduce Fan-Out)
# =====================================================================


def _coder_target(step: Any) -> str:
    """Pick the execution surface for a WBS step: the autonomous ReAct cell when the
    planner flagged it as needing iteration, else the one-shot coder (trivial path)."""
    return "agentic_cell" if step is not None and getattr(step, "requires_iteration", False) else "coder_agent"


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
            Send(_coder_target(step), {**state, "current_step_id": step.step_number})
            for step in parallel_tasks
        ]

    # RELAY: send exactly one pending step to protect VRAM
    first_pending = (
        next((t for t in mission_spec.tasks if t.status == "pending"), None)
        if mission_spec
        else None
    )
    target = _coder_target(first_pending)
    logger.info(
        "➡️  RELAY: provider=%s, ejecución secuencial → paso #%s (%s).",
        provider,
        first_pending.step_number if first_pending else "None",
        target,
    )
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="drift_monitor",
        target=target,
        reason=f"RELAY: provider={provider}, sequential execution",
        css=state.get("css"),
        tci=state.get("tci"),
    )
    return [Send(target, {**state, "current_step_id": first_pending.step_number if first_pending else None})]


# =====================================================================
# 4. GRAPH TOPOLOGY (Edges)
# =====================================================================
workflow.add_edge(START, "summarize_history")
workflow.add_edge("summarize_history", "session_delta_aggregator")
workflow.add_conditional_edges(
    "session_delta_aggregator", route_after_summarize, ["planner_agent", "ideation_loop"]
)
# The ideation loop no longer dead-ends: once the Socratic dialogue is distilled it
# hands the brief to the Actor-Critic PlannerAgent (run once, downstream of ideation
# and with planner_mode_active=False, so it never re-enters the loop). A mid-dialogue
# turn still suspends to END to await the next user response.
workflow.add_conditional_edges(
    "ideation_loop", route_after_ideation, ["planner_agent", END]
)
workflow.add_edge("planner_agent", "drift_monitor")
workflow.add_conditional_edges("drift_monitor", route_to_coders, ["coder_agent", "agentic_cell"])
# The ReAct cell loops back onto itself while its latest verdict says "continue" (each
# loop-back is a graph super-step → a Rewind-able checkpoint), and rejoins the normal
# downstream at contract_guard once it goes green or the iteration budget is spent.
workflow.add_conditional_edges(
    "agentic_cell", route_after_cell,
    {"agentic_cell": "agentic_cell", "contract_guard": "contract_guard"},
)
# ContractGuardNode is inserted as transparent middleware between
# CoderAgent and FinOpsGate. The node short-circuits internally (returns {} on
# quiet turns), so a routing callback would be cognitive noise. The node also
# owns contract_anchor mutation, which would have to be fragmented across the
# router otherwise — keeping it as a direct edge preserves a single ownership
# boundary for both the trigger evaluation and the anchor snapshot.
# coder_agent → contract_guard, unless the reflexion guard diverted to self-healing.
# error_correction proposes a HITL-gated fix (or concedes), then rejoins the normal path.
workflow.add_conditional_edges(
    "coder_agent", route_after_coder,
    {"error_correction": "error_correction", "contract_guard": "contract_guard"},
)
workflow.add_edge("error_correction", "contract_guard")
workflow.add_edge("contract_guard", "finops_gate")
# the finops_gate path-map is remapped from a list to a dict so the
# router's "apply_patch" verdict is rerouted through supervisor_node. This
# splices the Supervisor without touching brain/finops.py: route_after_finops
# still returns "apply_patch" / "__end__" unchanged.
workflow.add_conditional_edges(
    "finops_gate", route_after_finops,
    {"apply_patch": "supervisor_node", "__end__": END},
)
# supervisor_node terminates the graph on a budget hard-kill, else continues to
# apply_patch. route_after_supervisor reads the SESSION_BUDGET_HARD_KILL flag.
workflow.add_conditional_edges(
    "supervisor_node", route_after_supervisor,
    {"apply_patch": "apply_patch", "__end__": END},
)
workflow.add_edge("apply_patch", "validate_output")
workflow.add_conditional_edges("validate_output", route_after_validation, ["coder_agent", END])

# =====================================================================
# 5. COMPILATION WITH PERSISTENCE (CheckpointManager)
# =====================================================================
# We use checkpoint manager from brain/checkpoint.py to centralize the
# SQLite connection lifecycle management. The compiled instance
# `alienant_app` is imported by main.py and task_service.py.
alienant_app = workflow.compile(checkpointer=checkpoint_manager)

logger.info(
    "Compiled AILIENANT engine: "
    "SummarizeHistory → SessionDeltaAggregator → [PlannerAgent | IdeationLoop(Socratic)] → "
    "(IdeationLoop ─distilled→ PlannerAgent) → "
    "DriftMonitor → route_to_coders → CoderAgent(s) → ContractGuard → "
    "FinOpsGate → Supervisor → ApplyPatch → ValidateOutput."
)


# =====================================================================
# 6. CONTEXT ASSEMBLY UTILITIES
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
# 7. TOP-LEVEL ROUTING ENTRY POINT
# =====================================================================
# Re-exported from brain.intent_router so existing import sites
# (`from brain.engine import process_user_intent`) keep working unchanged.
# All three execution modes (SEQUENTIAL / MICRO_SWARM / FULL_SWARM) live there.
from brain.intent_router import process_user_intent  # noqa: E402,F401
