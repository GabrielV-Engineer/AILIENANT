# ailienant-core/brain/engine.py

import asyncio
import functools
import logging
import traceback as _tb
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar, cast

from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send
from langgraph.errors import GraphBubbleUp

from brain.state import AIlienantGraphState, assert_declared_channels, is_dispatchable
from brain.checkpoint import checkpoint_manager
from brain.failure_breaker import failure_breaker, normalize_signature
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS
from core.dead_letter import dead_letter_decorator  # DLQ node wrapper
from core.telemetry_log import log_node_transition
from shared.config import ENABLE_DYNAMIC_DISPATCH  # graph-construction-time topology gate

logger = logging.getLogger("AILIENANT_ENGINE")

# =====================================================================
# 1. GRAPH INITIALIZATION
# =====================================================================
workflow = StateGraph(AIlienantGraphState)

# =====================================================================
# 2. GRAPH NODES
# =====================================================================
# Deferred imports to avoid circular dependencies at startup.
from agents.planner import run_planner_node  # noqa: E402
from agents.researcher import run_researcher_node  # noqa: E402
from agents.coder import run_coder_node      # noqa: E402
from brain.summarizer import run_summarize_node  # noqa: E402
from brain.guardrails import run_validate_output_node, route_after_validation  # noqa: E402
from brain.drift_monitor import run_drift_compute_node, run_drift_gate_node  # noqa: E402
from brain.finops import run_finops_node, route_after_finops  # noqa: E402
from brain.nodes.aggregator_node import run_session_delta_aggregator_node  # noqa: E402
from agents.contract_guard import run_contract_guard_node  # noqa: E402
from core.supervisor import run_supervisor_node, route_after_supervisor  # noqa: E402
from agents.error_correction import run_error_correction_node  # noqa: E402 — self-healing reflexion node
# Autonomous ReAct execution cell. engine.py imports the node only — the MCTS edge it uses
# for branch governance lives entirely inside brain.agentic_cell, so the live graph spine
# never imports the offline tree directly.
from brain.agentic_cell import run_agentic_cell_node, route_after_cell  # noqa: E402
# Incremental per-step approval (13.0.9) — see brain/apply_gate.py's module
# docstring for the full PREPARE/GATE split rationale. Replaces the old
# apply_patch stub (a permanent `return {}` — the actual write lived entirely
# in core/task_service.py's post-graph replay) with two real nodes.
from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node  # noqa: E402


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
        project_id=state.get("project_id", ""),
        source="summarize_history",
        target=target,
        reason=reason,
    )
    logger.info("route_after_summarize: planner_mode_active=%s → %s.", state.get("planner_mode_active"), target)
    return target


def route_after_ideation(state: Dict[str, Any]) -> str:
    """Conditional edge after the Socratic ideation sub-graph.

    The ideation loop runs its Socratic rounds internally (each round pauses on
    native interrupt(), which suspends the whole run before this edge is ever
    reached) and exits only once it has distilled the conversation into a planner
    brief. We hand that brief to the autonomous PlannerAgent — its Actor-Critic
    reflection loop produces the schema-valid WBS — so the Socratic outcome never
    dead-ends at a zero-shot plan.

    ideation_synthesized=True → planner_agent (run the reflection loop on the brief)
    hitl_pending=True         → END (legacy end-of-turn suspend; no longer reachable
                                from the grill, kept for any other node that sets it)
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
        logger.error(
            "route_after_ideation: ideation_no_op — dialogue concluded without a "
            "synthesis handoff (shared_understanding_reached=%s, "
            "ideation_synthesized=%s). The turn dead-ends at END without ever "
            "reaching planner_agent; task_service.py surfaces this to the user "
            "as a planner failure, but the planner never ran.",
            state.get("shared_understanding_reached"),
            state.get("ideation_synthesized"),
        )
    log_routing_decision(
        session_id=state.get("task_id", ""),
        project_id=state.get("project_id", ""),
        source="ideation_loop",
        target=str(target),
        reason=reason,
    )
    logger.info("route_after_ideation: → %s (%s).", target, reason)
    return target


def route_after_planner(state: Dict[str, Any]) -> str:
    """Conditional edge: a PLAN_ONLY session stops the turn the instant the plan
    is produced, instead of falling through into drift_compute -> route_to_coders
    -> CoderAgent execution.

    agents/coder.py's RBAC gate (session_mode_from_channel + evaluate_action)
    already denies each individual write/execute action under PLAN_ONLY, but that
    only stops actions one at a time — it never stopped the graph from running
    and narrating every WBS step to completion in the same turn, which is what
    made a plan-mode task look like it was auto-executing unapproved. This edge
    stops the turn itself, matching the existing resubmit-under-write-capable-
    mode acceptance design (see agents/analyst.py's _AGREEMENT_SIGNALS): there is
    nothing further for this turn to safely do until the user decides.

    normalize_session_mode is required, not a raw comparison — the channel can
    still carry the deprecated "PLAN" alias, which a raw comparison against
    PLAN_ONLY would silently miss.
    """
    from core.permissions import SessionPermissionMode, normalize_session_mode, session_mode_from_channel
    from core.telemetry import log_routing_decision
    mode = normalize_session_mode(session_mode_from_channel(state.get("session_permission_mode")))
    if mode is SessionPermissionMode.PLAN_ONLY:
        target = END
        reason = f"session_permission_mode={mode.value} — plan broadcast; turn stops before execution"
    else:
        target = "drift_compute"
        reason = f"session_permission_mode={mode.value} — continuing to drift_compute"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        project_id=state.get("project_id", ""),
        source="planner_agent",
        target=str(target),
        reason=reason,
    )
    logger.info("route_after_planner: → %s (%s).", target, reason)
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
        result = await fn(state, *args, **kwargs)
        assert_declared_channels(name, result)
        return result

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
            except GraphBubbleUp:
                # A native interrupt() (GraphInterrupt, GraphDelegate, ParentCommand)
                # subclasses Exception and would otherwise be caught by the broad
                # handler below and converted into a healing_required signal — silently
                # destroying the pause instead of asking the user. A HITL suspension is
                # not a failure; let LangGraph's own suspend/resume machinery see it.
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
        project_id=state.get("project_id", ""),
        source="coder_agent",
        target=target,
        reason="healing_required" if healing else "coder_ok",
    )
    return target


workflow.add_node("summarize_history", _instrument_node("summarize_history", run_summarize_node))  # type: ignore[type-var]
# DLQ-wrapped node entrypoints. An unhandled exception promotes
# L1→L2 and persists a dead_letter_tasks row before re-raising (see
# core/dead_letter.py). The wrapper stack (_instrument_node, dead_letter_decorator,
# reflexion_guard) all use cast() or functools.wraps, which erases Generic precision;
# pyright: ignore[reportArgumentType] on each call is the accepted trade-off.
workflow.add_node("researcher_agent", _instrument_node("researcher_agent", dead_letter_decorator("researcher_agent")(run_researcher_node)))  # pyright: ignore[reportArgumentType]
workflow.add_node("planner_agent", _instrument_node("planner_agent", dead_letter_decorator("planner_agent")(run_planner_node)))  # pyright: ignore[reportArgumentType]
# DriftMonitor is split: drift_compute commits the (non-deterministic) similarity gate
# decision; drift_gate reads that committed decision and is the interrupt-bearing node
# (interrupt-first → replay-safe). See brain/drift_monitor.py.
workflow.add_node("drift_compute", _instrument_node("drift_compute", run_drift_compute_node))  # pyright: ignore[reportArgumentType]
workflow.add_node("drift_gate", _instrument_node("drift_gate", run_drift_gate_node))  # pyright: ignore[reportArgumentType]
# coder_agent is also wrapped by reflexion_guard (INSIDE the DLQ decorator): a fresh,
# in-budget failure becomes a healing signal routed to error_correction; an exhausted
# budget re-raises into the DLQ.
workflow.add_node("coder_agent", _instrument_node("coder_agent", dead_letter_decorator("coder_agent")(reflexion_guard("coder_agent")(run_coder_node))))  # pyright: ignore[reportArgumentType]
workflow.add_node("error_correction", _instrument_node("error_correction", dead_letter_decorator("error_correction")(run_error_correction_node)))  # pyright: ignore[reportArgumentType]
# apply_patch (PREPARE) / apply_commit (GATE) — 13.0.9 incremental per-step
# approval, see brain/apply_gate.py's module docstring. apply_commit is
# deliberately NOT reflexion_guard-wrapped: a native interrupt() raised inside
# it must reach LangGraph's own suspend/resume machinery untouched, never be
# converted into a healing signal (dead_letter_decorator already re-raises
# GraphBubbleUp — see core/dead_letter.py — so wrapping it here is safe).
workflow.add_node("apply_patch", _instrument_node("apply_patch", dead_letter_decorator("apply_patch")(run_apply_prepare_node)))  # pyright: ignore[reportArgumentType]
workflow.add_node("apply_commit", _instrument_node("apply_commit", dead_letter_decorator("apply_commit")(run_apply_commit_node)))  # pyright: ignore[reportArgumentType]
workflow.add_node("validate_output", _instrument_node("validate_output", dead_letter_decorator("validate_output")(run_validate_output_node)))  # pyright: ignore[reportArgumentType]
workflow.add_node("finops_gate", _instrument_node("finops_gate", run_finops_node))  # pyright: ignore[reportArgumentType]
workflow.add_node("ideation_loop", ideation_graph)  # pyright: ignore[reportArgumentType]
workflow.add_node("session_delta_aggregator", _instrument_node("session_delta_aggregator", run_session_delta_aggregator_node))  # pyright: ignore[reportArgumentType]
workflow.add_node("contract_guard", _instrument_node("contract_guard", run_contract_guard_node))  # pyright: ignore[reportArgumentType]
# deterministic FinOps Supervisor spliced between finops_gate and
# apply_patch. DLQ-wrapped an AuditChainBrokenError becomes a
# recoverable dead_letter_tasks episode rather than a silent graph death.
workflow.add_node(  # pyright: ignore[reportArgumentType]
    "supervisor_node",
    _instrument_node("supervisor_node", dead_letter_decorator("supervisor_node")(run_supervisor_node)),
)
# Autonomous ReAct cell — same wrapper stack as coder_agent (DLQ + instrumentation).
# A non-converging loop concedes gracefully inside the node, so it does not need the
# reflexion guard; an unexpected fault still promotes to the DLQ.
workflow.add_node(  # pyright: ignore[reportArgumentType]
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
            "🔀 SWARM: provider=CLOUD, fan-out → %d CoderAgent(s) in parallel.",
            len(parallel_tasks),
        )
        log_routing_decision(
            session_id=state.get("task_id", ""),
            project_id=state.get("project_id", ""),
            source="drift_monitor",
            target="coder_agent",
            reason=f"SWARM: provider=CLOUD, {len(parallel_tasks)} tasks in parallel",
            css=state.get("css"),
            tci=state.get("tci"),
        )
        # Explicit state augmentation: each fan-out node carries its own step's
        # role in its immutable payload, so per-step tool selection is scoped to
        # the step that runs there rather than the task-initial role.
        return [
            Send(
                _coder_target(step),
                {
                    **state,
                    "active_role": step.target_role,
                    "current_step_id": step.step_number,
                },
            )
            for step in parallel_tasks
        ]

    # RELAY: send exactly one pending step to protect VRAM
    first_pending = (
        # is_dispatchable also selects revision_requested — a step the human
        # asked to be regenerated with feedback. Must move in lockstep with
        # guardrails.py's stall guard and advance predicate (brain/state.py).
        # Passing the full task list lets it enforce depends_on (DEBT-197).
        next((t for t in mission_spec.tasks if is_dispatchable(t, mission_spec.tasks)), None)
        if mission_spec
        else None
    )
    target = _coder_target(first_pending)
    logger.info(
        "➡️  RELAY: provider=%s, sequential execution → step #%s (%s).",
        provider,
        first_pending.step_number if first_pending else "None",
        target,
    )
    log_routing_decision(
        session_id=state.get("task_id", ""),
        project_id=state.get("project_id", ""),
        source="drift_monitor",
        target=target,
        reason=f"RELAY: provider={provider}, sequential execution",
        css=state.get("css"),
        tci=state.get("tci"),
    )
    # Explicit state augmentation: surface the pending step's role on the payload
    # so the single relayed node selects tools under the role that step runs as,
    # not whatever role the task entered with.
    return [
        Send(
            target,
            {
                **state,
                "active_role": (
                    first_pending.target_role
                    if first_pending
                    else state.get("active_role")
                ),
                "current_step_id": (
                    first_pending.step_number if first_pending else None
                ),
            },
        )
    ]


# =====================================================================
# 3b. DYNAMIC DISPATCH (opt-in, feature-flagged at construction time)
# =====================================================================
# The ENABLE_DYNAMIC_DISPATCH flag is read once here, at module import / graph
# construction. When it is off (the default), none of the dispatch nodes or edges are
# added and the compiled graph is topologically identical to a deployment without this
# feature — production sets the env before the process starts; tests reload this module
# under a patched environment to rebuild the graph. The new edges out of the two dispatch
# origins fire only when a `dispatch_plan` is present, so an enabled deployment that never
# emits a plan still takes exactly the pre-existing path.


def _route_planner_dispatch(state: Dict[str, Any]) -> str:
    """Planner exit: a PLAN_ONLY session stops the turn (mirrors route_after_planner
    below, which owns the non-dispatch path); otherwise fan out to the dispatch
    subgraph when a plan was emitted, else the normal successor (drift_compute)."""
    from core.permissions import SessionPermissionMode, normalize_session_mode, session_mode_from_channel
    mode = normalize_session_mode(session_mode_from_channel(state.get("session_permission_mode")))
    if mode is SessionPermissionMode.PLAN_ONLY:
        return END
    return "dispatch_origin" if state.get("dispatch_plan") else "drift_compute"


def _route_researcher_dispatch(state: Dict[str, Any]) -> str:
    """Researcher exit: fan out to the dispatch subgraph when a plan was emitted, else the
    normal successor (planner_agent)."""
    return "dispatch_origin" if state.get("dispatch_plan") else "planner_agent"


def _wire_dynamic_dispatch(wf: "StateGraph") -> None:
    """Add the dispatch subgraph nodes and edges. Called only when the feature is enabled.

    Imports are deferred here so a disabled deployment never loads the dispatch machinery.
    The pure sync nodes (origin/fanout/gate/advance) are added raw; the async worker and
    synthesize nodes are telemetry-wrapped like the rest of the spine.
    """
    from brain.dispatch import (
        dispatch_advance,
        dispatch_fanout,
        dispatch_gate,
        dispatch_origin,
        dispatch_router,
        route_after_admission,
        route_after_synthesis,
        route_after_workers,
    )
    from brain.nodes.dispatch_synthesize_node import dispatch_synthesize
    from brain.nodes.subagent_worker_node import subagent_worker

    wf.add_node("dispatch_origin", dispatch_origin)  # pyright: ignore[reportArgumentType]
    wf.add_node("dispatch_fanout", dispatch_fanout)  # pyright: ignore[reportArgumentType]
    wf.add_node("dispatch_gate", dispatch_gate)      # pyright: ignore[reportArgumentType]
    wf.add_node("dispatch_advance", dispatch_advance)  # pyright: ignore[reportArgumentType]
    wf.add_node("subagent_worker", _instrument_node("subagent_worker", subagent_worker))  # pyright: ignore[reportArgumentType]
    wf.add_node("dispatch_synthesize", _instrument_node("dispatch_synthesize", dispatch_synthesize))  # pyright: ignore[reportArgumentType]

    # Admission gate → Send-only fan-out, or short-circuit to synthesis on a denial.
    wf.add_conditional_edges(
        "dispatch_origin", route_after_admission,
        {"dispatch_fanout": "dispatch_fanout", "dispatch_synthesize": "dispatch_synthesize"},
    )
    wf.add_conditional_edges("dispatch_fanout", dispatch_router, ["subagent_worker"])
    wf.add_edge("subagent_worker", "dispatch_gate")
    # Next wave (same round) → origin; new round → advance; done → synthesize.
    wf.add_conditional_edges(
        "dispatch_gate", route_after_workers,
        {
            "dispatch_origin": "dispatch_origin",
            "dispatch_advance": "dispatch_advance",
            "dispatch_synthesize": "dispatch_synthesize",
        },
    )
    wf.add_edge("dispatch_advance", "dispatch_origin")
    # Terminal: rejoin the spine at whichever node the emitting agent recorded.
    wf.add_conditional_edges(
        "dispatch_synthesize", route_after_synthesis,
        {"drift_compute": "drift_compute", "planner_agent": "planner_agent"},
    )


# =====================================================================
# 4. GRAPH TOPOLOGY (Edges)
# =====================================================================
workflow.add_edge(START, "summarize_history")
workflow.add_edge("summarize_history", "session_delta_aggregator")
# The ResearcherAgent is spliced in front of every PlannerAgent entry: the router
# verdicts are unchanged ("planner_agent"), but the path-map reroutes that verdict
# through researcher_agent first, which owns all retrieval + the routing cascade and
# emits the signal the Planner consumes. researcher_agent → planner_agent closes it.
workflow.add_conditional_edges(
    "session_delta_aggregator", route_after_summarize,
    {"planner_agent": "researcher_agent", "ideation_loop": "ideation_loop"},
)
# The ideation loop no longer dead-ends: once the Socratic dialogue is distilled it
# hands the brief to the Actor-Critic PlannerAgent (run once, downstream of ideation
# and with planner_mode_active=False, so it never re-enters the loop). A mid-dialogue
# turn still suspends to END to await the next user response.
workflow.add_conditional_edges(
    "ideation_loop", route_after_ideation, {"planner_agent": "researcher_agent", END: END}
)
if ENABLE_DYNAMIC_DISPATCH:
    # Additive/opt-in: the two dispatch origins get a conditional exit that fans out to
    # the dispatch subgraph only when a dispatch_plan is present, else returns the exact
    # pre-8.15 target. The subgraph rejoins the spine via route_after_synthesis.
    workflow.add_conditional_edges(
        "researcher_agent", _route_researcher_dispatch,
        {"dispatch_origin": "dispatch_origin", "planner_agent": "planner_agent"},
    )
    workflow.add_conditional_edges(
        "planner_agent", _route_planner_dispatch,
        {"dispatch_origin": "dispatch_origin", "drift_compute": "drift_compute", END: END},
    )
    _wire_dynamic_dispatch(workflow)
else:
    workflow.add_edge("researcher_agent", "planner_agent")
    workflow.add_conditional_edges(
        "planner_agent", route_after_planner, {"drift_compute": "drift_compute", END: END},
    )
workflow.add_edge("drift_compute", "drift_gate")
workflow.add_conditional_edges("drift_gate", route_to_coders, ["coder_agent", "agentic_cell"])
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
# apply_patch (PREPARE, no interrupt) -> apply_commit (GATE, interrupt-first).
# Unconditional edge — apply_commit's own first statement
# (`if not state.get("pending_apply"): return {}`) is the routing, mirroring
# drift_compute -> drift_gate's identical shape.
workflow.add_edge("apply_patch", "apply_commit")
workflow.add_edge("apply_commit", "validate_output")
# validate_output → retry the same step (coder_agent) · advance to the next pending
# WBS step (drift_gate re-runs route_to_coders and re-checks finops/budget) ·
# self-heal a run_command failure surfaced downstream in the apply gate
# (error_correction, 13.0.9) · END.
workflow.add_conditional_edges(
    "validate_output", route_after_validation,
    ["coder_agent", "drift_gate", "error_correction", END],
)

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
