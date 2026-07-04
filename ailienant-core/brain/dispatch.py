# ailienant-core/brain/dispatch.py
"""Generalized Send()-fanout dispatch primitive for dynamic subagents.

The structured analog of ``route_to_coders``'s SWARM fan-out: given a validated
``DispatchPlan``, emit one ``Send`` per ``SubagentTask`` so every task runs as a
parallel ``subagent_worker`` branch. Kept as a separate function with its own call
site so the production SWARM/RELAY routing is never touched.

Admission is gated at ``dispatch_origin`` (a node, so it can write state): it
validates the plan, re-checks the depth/width ceilings independently of the Pydantic
bounds (defense in depth — never silently truncating a rejected plan), and reserves
the wave's estimated budget against the task's ``max_budget_usd``/``current_cost_usd``
channels. The verdict routes either to the Send-only fan-out (``dispatch_fanout``) or
straight to synthesis with ``denied``/``budget_exhausted`` envelopes.

A plan wider than ``MAX_CONCURRENT_SUBAGENTS`` is split into sequential waves within a
round; patterns that need a *new* task set (an adversarial critic, a loop continuation)
advance to a fresh round via ``dispatch_advance``. Rounds are bounded by
``MAX_DISPATCH_ROUNDS`` and the budget backstop; each dispatch's results are isolated by
a consume-watermark so a second dispatch in the same run never re-digests the first's.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping
from uuid import uuid4

from langgraph.constants import Send

from shared.config import (
    MAX_CONCURRENT_SUBAGENTS,
    MAX_DISPATCH_DEPTH,
    MAX_DISPATCH_ROUNDS,
    MAX_DISPATCH_WIDTH,
    MAX_OBSERVATION_CHARS,
)
from brain.subagent_contracts import DispatchPlan, SubagentResultEnvelope

logger = logging.getLogger("DISPATCH")

# The graph node names this module wires together. Kept as constants so the
# harness graph and the production wiring name the same vertices.
SUBAGENT_WORKER_NODE = "subagent_worker"
DISPATCH_ORIGIN_NODE = "dispatch_origin"
DISPATCH_FANOUT_NODE = "dispatch_fanout"
DISPATCH_GATE_NODE = "dispatch_gate"
DISPATCH_ADVANCE_NODE = "dispatch_advance"
DISPATCH_SYNTHESIZE_NODE = "dispatch_synthesize"

# Fallback downstream target if a dispatch was entered without setting the return
# node — the subgraph must never dangle with nowhere to rejoin.
_DEFAULT_RETURN_NODE = "drift_compute"

# Patterns whose orchestration may introduce a second task set (a new round) rather
# than merely splitting one fixed task list into waves.
_MULTI_ROUND_PATTERNS = frozenset({"adversarial_verification", "loop_until_done"})

# The max_length on SubagentTask.description (kept in sync with the contract).
_MAX_DESCRIPTION_CHARS = 4000


def build_dispatch_sends(
    plan: DispatchPlan,
    base_state: Mapping[str, Any],
    *,
    target_node: str = SUBAGENT_WORKER_NODE,
) -> List[Send]:
    """One ``Send`` per ``SubagentTask`` in the current wave.

    Mirrors ``route_to_coders``'s payload-augmentation idiom (``{**state, ...}``) as
    a pattern, not shared code. ``dispatch_depth`` is incremented here — at the
    fan-out edge, never inside the worker — so every recursion level is accounted for
    independently of the Pydantic bound on ``DispatchPlan.dispatch_depth``. The wave
    slice is selected by ``dispatch_wave_count`` so a later wave picks up where the
    previous one stopped. An inline depth guard denies (returns no Sends) past the
    recursion ceiling as defense in depth for a caller that bypassed the gate.
    """
    if int(base_state.get("dispatch_depth", 0) or 0) >= MAX_DISPATCH_DEPTH:
        logger.warning("build_dispatch_sends: dispatch_depth ceiling reached; no fan-out.")
        return []
    wave = int(base_state.get("dispatch_wave_count", 0) or 0)
    cap = MAX_CONCURRENT_SUBAGENTS
    wave_tasks = plan.tasks[wave * cap : (wave + 1) * cap]
    next_depth = int(base_state.get("dispatch_depth", 0) or 0) + 1
    return [
        Send(
            target_node,
            {
                **base_state,
                "_dispatch_task": task.model_dump(),
                "dispatch_depth": next_depth,
            },
        )
        for task in wave_tasks
    ]


def _denied_envelopes(raw_tasks: Any, status: str, message: str) -> List[Dict[str, Any]]:
    """Build at most ``MAX_DISPATCH_WIDTH`` rejection envelopes.

    The admission gate must NEVER iterate an unbounded raw task list — an over-scoped
    plan (thousands of tasks) would otherwise materialize thousands of Pydantic models
    and block the event loop / overflow the checkpoint. Slicing to the width ceiling is
    mathematically sufficient for the orchestrator to see the wave was rejected.
    """
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    out: List[Dict[str, Any]] = []
    for raw in tasks[:MAX_DISPATCH_WIDTH]:
        task_id = str(raw.get("task_id", "")) if isinstance(raw, Mapping) else ""
        out.append(
            SubagentResultEnvelope(
                task_id=task_id,
                status=status,  # type: ignore[arg-type]  # constrained by caller
                raw_digest="",
                error_message=message,
            ).model_dump()
        )
    return out


def dispatch_origin(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Admission gate + wave loop-back target.

    Validates the plan, re-checks depth/width (bounded rejection), and reserves the
    round's budget single-flight. Writes ``_dispatch_admission`` (read by
    ``route_after_admission``) and, on rejection, the bounded rejection envelopes; on a
    fresh admitted round, folds the reservation into ``current_cost_usd``.
    """
    raw_plan = state.get("dispatch_plan") or {}
    raw_tasks = raw_plan.get("tasks") or []

    try:
        plan = DispatchPlan.model_validate(raw_plan)
    except Exception as exc:  # noqa: BLE001 — a malformed/over-cap plan is a denial, not a crash
        logger.warning("dispatch_origin denied a malformed/over-cap plan: %s", exc)
        return {
            "_dispatch_admission": "denied",
            "_dispatch_results": _denied_envelopes(raw_tasks, "denied", f"invalid dispatch plan: {exc}"),
        }

    depth = int(state.get("dispatch_depth", 0) or 0)
    if depth >= MAX_DISPATCH_DEPTH or len(plan.tasks) > MAX_DISPATCH_WIDTH:
        logger.warning(
            "dispatch_origin denied: depth=%d (max %d) width=%d (max %d)",
            depth, MAX_DISPATCH_DEPTH, len(plan.tasks), MAX_DISPATCH_WIDTH,
        )
        return {
            "_dispatch_admission": "denied",
            "_dispatch_results": _denied_envelopes(
                raw_tasks, "denied", "dispatch depth/width ceiling exceeded"
            ),
        }

    # Budget reservation is single-flight per round: only the first wave of a round
    # (wave_count == 0) that has not yet been reserved reserves. An unset marker reads
    # as -1 so round 0 reserves exactly once (replay-safe, Charter §5.3).
    wave = int(state.get("dispatch_wave_count", 0) or 0)
    round_count = int(state.get("dispatch_round_count", 0) or 0)
    reserved_marker = state.get("_dispatch_reserved_round")
    reserved_round = int(reserved_marker) if reserved_marker is not None else -1
    max_budget = float(state.get("max_budget_usd", 0.0) or 0.0)

    if wave != 0 or round_count <= reserved_round or max_budget <= 0.0:
        # A later wave, an already-reserved round, or no configured budget ceiling —
        # nothing to reserve; finops remains the hard backstop.
        return {"_dispatch_admission": "admit"}

    from brain.dispatch_ledger import estimate_wave_cost, reserve_dispatch_budget

    estimate = estimate_wave_cost(raw_tasks)  # raw dicts — the estimator reads .get()
    reservation = reserve_dispatch_budget(
        task_id=str(state.get("task_id", "")),
        batch_id=str(raw_plan.get("batch_id", "") or uuid4().hex),
        estimated_cost_usd=estimate,
        current_cost_usd=float(state.get("current_cost_usd", 0.0) or 0.0),
        max_budget_usd=max_budget,
    )
    if reservation is None:
        return {
            "_dispatch_admission": "budget_exhausted",
            "_dispatch_results": _denied_envelopes(
                raw_tasks, "budget_exhausted", "dispatch budget exhausted"
            ),
        }
    return {
        "_dispatch_admission": "admit",
        # Fold the reservation into the authoritative cost channel (operator.add);
        # synthesis commits the actual and refunds the unused delta.
        "current_cost_usd": reservation.reserved_usd,
        "_dispatch_reserved_usd": float(state.get("_dispatch_reserved_usd", 0.0) or 0.0)
        + reservation.reserved_usd,
        "_dispatch_reserved_round": round_count,
    }


def route_after_admission(state: Mapping[str, Any]) -> str:
    """String router: fan out only on an admit verdict, else short-circuit to synthesis.

    Kept a pure string router (never a mixed Send/str return) so the fan-out edge and
    the deny edge never share one router — the deny path's rejection envelopes are
    already recorded, so synthesis simply digests them.
    """
    return (
        DISPATCH_FANOUT_NODE
        if state.get("_dispatch_admission") == "admit"
        else DISPATCH_SYNTHESIZE_NODE
    )


def dispatch_fanout(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Pass-through node that owns the Send-only fan-out edge (``dispatch_router``)."""
    return {}


def dispatch_router(state: Mapping[str, Any]) -> List[Send]:
    """Conditional-edge adapter: reconstruct the plan from state and fan out.

    Reached only via ``dispatch_fanout`` (i.e. after admission), so an absent plan just
    yields no Sends. ``build_dispatch_sends`` takes a typed ``DispatchPlan``.
    """
    raw_plan = state.get("dispatch_plan")
    if not raw_plan:
        return []
    plan = DispatchPlan.model_validate(raw_plan)
    return build_dispatch_sends(plan, state)


def dispatch_gate(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Fan-in barrier node — runs once after a wave's workers rejoin.

    Reached from ``subagent_worker`` by a plain edge, so LangGraph collapses the N
    parallel branches into a single invocation here (unlike a conditional edge on the
    worker, which would fire once per branch). Advances the wave counter so the next
    wave selects the following task slice.
    """
    return {"dispatch_wave_count": int(state.get("dispatch_wave_count", 0) or 0) + 1}


def _budget_exhausted(state: Mapping[str, Any]) -> bool:
    """Budget backstop for the pattern loop — the blueprint's 'budget is the backstop
    even when logic caps fail'. No configured ceiling (<=0) means unmetered."""
    max_budget = float(state.get("max_budget_usd", 0.0) or 0.0)
    if max_budget <= 0.0:
        return False
    return float(state.get("current_cost_usd", 0.0) or 0.0) >= max_budget


def _loop_done(state: Mapping[str, Any]) -> bool:
    """loop_until_done halts when any envelope since the watermark reports done=True."""
    consumed = int(state.get("_dispatch_consumed", 0) or 0)
    results = list(state.get("_dispatch_results") or [])[consumed:]
    for env in results:
        structured = env.get("structured_result") if isinstance(env, Mapping) else None
        if isinstance(structured, Mapping) and bool(structured.get("done")):
            return True
    return False


def _pattern_needs_another_round(state: Mapping[str, Any]) -> bool:
    """Whether the current pattern should advance to a new round (mechanism B)."""
    raw_plan = state.get("dispatch_plan") or {}
    pattern = str(raw_plan.get("pattern", ""))
    if pattern not in _MULTI_ROUND_PATTERNS:
        return False
    round_count = int(state.get("dispatch_round_count", 0) or 0)
    if round_count >= MAX_DISPATCH_ROUNDS or _budget_exhausted(state):
        return False
    if pattern == "adversarial_verification":
        # Exactly one critic round after the producers.
        return round_count == 0
    return not _loop_done(state)


def route_after_workers(state: Mapping[str, Any]) -> str:
    """Loop back for the next wave, advance to a new round, or proceed to synthesis.

    ``dispatch_wave_count`` strictly increases each wave (via ``dispatch_gate``) and
    ``dispatch_round_count`` strictly increases each round (via ``dispatch_advance``),
    both bounded, so the loop always terminates.
    """
    waves_done = int(state.get("dispatch_wave_count", 0) or 0)
    raw_plan = state.get("dispatch_plan") or {}
    total = len(raw_plan.get("tasks", []))
    if waves_done * MAX_CONCURRENT_SUBAGENTS < total:
        return DISPATCH_ORIGIN_NODE
    if _pattern_needs_another_round(state):
        return DISPATCH_ADVANCE_NODE
    return DISPATCH_SYNTHESIZE_NODE


def _build_critic_plan(state: Mapping[str, Any], raw_plan: Mapping[str, Any]) -> Dict[str, Any]:
    """A single adversarial ``analyst_readonly`` critic task reviewing the producers.

    The producers' digests are passed through the critic task's ``description`` (bounded)
    — never ``context_refs``, which the contract restricts to VFS paths, never raw
    content. The critic is READ_ONLY-floored, so it cannot mutate what it judges.
    """
    consumed = int(state.get("_dispatch_consumed", 0) or 0)
    results = list(state.get("_dispatch_results") or [])[consumed:]
    digest = "\n\n".join(
        f"[{r.get('task_id', '')}] {str(r.get('raw_digest', ''))}"
        for r in results
        if isinstance(r, Mapping)
    )[:MAX_OBSERVATION_CHARS]
    description = (
        "You are an adversarial verifier. Critically review the producer outputs "
        "below and return a verdict on whether they are acceptable.\n\n" + digest
    )[:_MAX_DESCRIPTION_CHARS]
    critic_task = {
        "task_id": uuid4().hex,
        "description": description,
        "subagent_role": "analyst_readonly",
        "response_schema": {
            "fields": [
                {"name": "verdict", "type": "str", "description": "justification for the ruling"},
                {"name": "passed", "type": "bool", "description": "true if producer outputs are acceptable"},
            ]
        },
        "context_refs": [],
        "max_iterations": 1,
    }
    return {
        "pattern": "adversarial_verification",
        "tasks": [critic_task],
        "synthesis_instruction": str(raw_plan.get("synthesis_instruction", "")),
        "dispatch_depth": int(raw_plan.get("dispatch_depth", 0) or 0),
    }


def dispatch_advance(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Round transition (mechanism B): swap in the next round's plan and reset the wave.

    ``dispatch_wave_count`` resets to 0 so the new plan slices from its first task;
    ``dispatch_round_count`` increments so the reservation single-flight and the round
    ceiling advance. For adversarial verification the follow-up is the critic plan; for
    loop_until_done it re-issues the same tasks for another pass.
    """
    raw_plan = state.get("dispatch_plan") or {}
    pattern = str(raw_plan.get("pattern", ""))
    if pattern == "adversarial_verification":
        followup: Dict[str, Any] = _build_critic_plan(state, raw_plan)
    else:  # loop_until_done — another pass over the same tasks
        followup = dict(raw_plan)
    return {
        "dispatch_plan": followup,
        "dispatch_wave_count": 0,
        "dispatch_round_count": int(state.get("dispatch_round_count", 0) or 0) + 1,
    }


def route_after_synthesis(state: Mapping[str, Any]) -> str:
    """Return to the node the emitting agent recorded, defaulting safely."""
    return str(state.get("dispatch_return_node") or _DEFAULT_RETURN_NODE)
