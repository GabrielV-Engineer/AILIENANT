# ailienant-core/brain/dispatch.py
"""Generalized Send()-fanout dispatch primitive for dynamic subagents.

The structured analog of ``route_to_coders``'s SWARM fan-out: given a validated
``DispatchPlan``, emit one ``Send`` per ``SubagentTask`` so every task runs as a
parallel ``subagent_worker`` branch. Kept as a separate function with its own call
site so the production SWARM/RELAY routing is never touched.

A plan wider than ``MAX_CONCURRENT_SUBAGENTS`` is split into sequential waves: each
super-step fans out at most one wave, ``dispatch_gate`` advances the wave counter at
the fan-in barrier, and ``route_after_workers`` loops back for the next wave until the
plan is exhausted, then hands off to synthesis. Peak concurrency stays bounded by the
cap without any runtime semaphore fighting the graph's native parallelism.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from langgraph.constants import Send

from shared.config import MAX_CONCURRENT_SUBAGENTS
from brain.subagent_contracts import DispatchPlan

# The graph node names this module wires together. Kept as constants so the
# harness graph and the eventual production wiring name the same vertices.
SUBAGENT_WORKER_NODE = "subagent_worker"
DISPATCH_ORIGIN_NODE = "dispatch_origin"
DISPATCH_SYNTHESIZE_NODE = "dispatch_synthesize"


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
    previous one stopped.
    """
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


def dispatch_router(state: Mapping[str, Any]) -> List[Send]:
    """Conditional-edge adapter: reconstruct the plan from state and fan out.

    LangGraph calls a conditional edge with the state dict; ``build_dispatch_sends``
    takes a typed ``DispatchPlan``, so this thin wrapper bridges the two. An absent
    plan yields no Sends (the edge simply produces nothing to run).
    """
    raw_plan = state.get("dispatch_plan")
    if not raw_plan:
        return []
    plan = DispatchPlan.model_validate(raw_plan)
    return build_dispatch_sends(plan, state)


def dispatch_origin(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Fan-out origin / wave loop-back target.

    A pass-through node whose outgoing conditional edge is ``dispatch_router``. It
    owns no state mutation — the wave counter is advanced at the fan-in barrier by
    ``dispatch_gate`` so the increment happens exactly once per wave.
    """
    return {}


def dispatch_gate(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Fan-in barrier node — runs once after a wave's workers rejoin.

    Reached from ``subagent_worker`` by a plain edge, so LangGraph collapses the N
    parallel branches into a single invocation here (unlike a conditional edge on the
    worker, which would fire once per branch). Advances the wave counter so the next
    wave selects the following task slice.
    """
    return {"dispatch_wave_count": int(state.get("dispatch_wave_count", 0) or 0) + 1}


def route_after_workers(state: Mapping[str, Any]) -> str:
    """Loop back for the next wave, or proceed to synthesis when the plan is spent.

    Mirrors ``route_after_cell``'s bounded self-loop shape. ``dispatch_wave_count``
    strictly increases each pass (via ``dispatch_gate``), so the loop always
    terminates once every task slice has been dispatched.
    """
    waves_done = int(state.get("dispatch_wave_count", 0) or 0)
    raw_plan = state.get("dispatch_plan") or {}
    total = len(raw_plan.get("tasks", []))
    if waves_done * MAX_CONCURRENT_SUBAGENTS < total:
        return DISPATCH_ORIGIN_NODE
    return DISPATCH_SYNTHESIZE_NODE
