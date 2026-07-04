# ailienant-core/tests/test_dispatch_wiring.py
"""8.15.5 DoD — dynamic dispatch wired into the engine graph.

Covers the six canonical patterns exercised end-to-end on a compiled dispatch subgraph,
the recursion/width/budget denial paths (bounded, never silently truncated), the
analyst_readonly READ_ONLY floor-lock under every session mode, the result-isolation
watermark across two dispatches in one run, and the feature-flag topology identity
(flag-off compiled graph has none of the dispatch nodes; flag-on has all of them).
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, List, cast
from unittest import mock

import pytest
from langgraph.graph import END, START, StateGraph

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
from brain.state import AIlienantGraphState
from brain.subagent_contracts import DispatchPlan, SubagentResponseSchema, SubagentTask
from shared.config import MAX_DISPATCH_ROUNDS, MAX_DISPATCH_WIDTH


# ── Harness: the production dispatch subgraph, entered at the origin ──────────


def _build_harness() -> Any:
    g: StateGraph = StateGraph(AIlienantGraphState)
    g.add_node("dispatch_origin", cast(Any, dispatch_origin))
    g.add_node("dispatch_fanout", cast(Any, dispatch_fanout))
    g.add_node("subagent_worker", cast(Any, subagent_worker))
    g.add_node("dispatch_gate", cast(Any, dispatch_gate))
    g.add_node("dispatch_advance", cast(Any, dispatch_advance))
    g.add_node("dispatch_synthesize", cast(Any, dispatch_synthesize))
    g.add_edge(START, "dispatch_origin")
    g.add_conditional_edges(
        "dispatch_origin", route_after_admission,
        {"dispatch_fanout": "dispatch_fanout", "dispatch_synthesize": "dispatch_synthesize"},
    )
    g.add_conditional_edges("dispatch_fanout", dispatch_router, ["subagent_worker"])
    g.add_edge("subagent_worker", "dispatch_gate")
    g.add_conditional_edges(
        "dispatch_gate", route_after_workers,
        {
            "dispatch_origin": "dispatch_origin",
            "dispatch_advance": "dispatch_advance",
            "dispatch_synthesize": "dispatch_synthesize",
        },
    )
    g.add_edge("dispatch_advance", "dispatch_origin")
    g.add_conditional_edges(
        "dispatch_synthesize", route_after_synthesis,
        {"drift_compute": END, "planner_agent": END},
    )
    return g.compile()


def _schema(*fields: Any) -> Dict[str, Any]:
    return {"fields": [{"name": n, "type": t, "description": d} for (n, t, d) in fields]}


def _task(i: int, role: str = "core_dev", schema: Any = None) -> Dict[str, Any]:
    return {
        "task_id": f"t{i}",
        "description": f"do work {i}",
        "subagent_role": role,
        "response_schema": schema or _schema(("summary", "str", "one line")),
        "context_refs": [],
        "max_iterations": 1,
    }


def _plan(pattern: str, tasks: List[Dict[str, Any]], depth: int = 0) -> Dict[str, Any]:
    return {
        "pattern": pattern,
        "tasks": tasks,
        "synthesis_instruction": "merge",
        "dispatch_depth": depth,
    }


async def _echo_answer(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    """Generic schema-satisfying answer: one valid value per declared field."""
    defaults = {"str": "ok", "int": 1, "float": 1.0, "bool": True, "list_str": ["x"]}
    return {f.name: defaults[f.type] for f in task.response_schema.fields}


async def _scored_answer(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    n = int("".join(ch for ch in task.task_id if ch.isdigit()) or "0")
    return {"summary": f"cand {task.task_id}", "score": float(n)}


async def _notdone_answer(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    return {"summary": "still going", "done": False}


def _seed(plan: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {"dispatch_plan": plan, "session_permission_mode": "READ_ONLY"}
    state.update(extra)
    return state


def _cfg(answer_fn: Any = _echo_answer) -> Dict[str, Any]:
    return {"configurable": {"dispatch_answer_fn": answer_fn}, "recursion_limit": 100}


# ── The six canonical patterns, end-to-end ───────────────────────────────────


@pytest.mark.anyio
async def test_pattern_classify_and_act() -> None:
    app = _build_harness()
    out = await app.ainvoke(cast(Any, _seed(_plan("classify_and_act", [_task(0)]))), _cfg())
    batch = out["dispatch_batch_result"]
    assert batch["pattern"] == "classify_and_act"
    assert len(batch["results"]) == 1
    assert batch["results"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_pattern_fanout_and_synthesize() -> None:
    app = _build_harness()
    out = await app.ainvoke(
        cast(Any, _seed(_plan("fanout_and_synthesize", [_task(i) for i in range(3)]))), _cfg()
    )
    batch = out["dispatch_batch_result"]
    assert len(batch["results"]) == 3
    assert all(r["status"] == "ok" for r in batch["results"])
    assert batch["winner_task_id"] is None


@pytest.mark.anyio
async def test_pattern_tournament_selects_highest_score() -> None:
    app = _build_harness()
    tasks = [_task(i, schema=_schema(("summary", "str", "s"), ("score", "float", "n"))) for i in range(3)]
    out = await app.ainvoke(cast(Any, _seed(_plan("tournament", tasks))), _cfg(_scored_answer))
    assert out["dispatch_batch_result"]["winner_task_id"] == "t2"  # highest score


@pytest.mark.anyio
async def test_pattern_generate_and_filter_picks_winner() -> None:
    app = _build_harness()
    tasks = [_task(i, schema=_schema(("summary", "str", "s"), ("score", "float", "n"))) for i in range(2)]
    out = await app.ainvoke(cast(Any, _seed(_plan("generate_and_filter", tasks))), _cfg(_scored_answer))
    assert out["dispatch_batch_result"]["winner_task_id"] == "t1"


@pytest.mark.anyio
async def test_pattern_adversarial_verification_adds_critic_round() -> None:
    app = _build_harness()
    producers = [_task(i) for i in range(2)]
    out = await app.ainvoke(cast(Any, _seed(_plan("adversarial_verification", producers))), _cfg())
    # Two producer envelopes + one analyst_readonly critic envelope.
    assert out["dispatch_round_count"] == 1
    assert len(out["_dispatch_results"]) == 3
    assert out["dispatch_batch_result"]["pattern"] == "adversarial_verification"


@pytest.mark.anyio
async def test_pattern_loop_until_done_is_bounded() -> None:
    app = _build_harness()
    out = await app.ainvoke(
        cast(Any, _seed(_plan("loop_until_done", [_task(0, schema=_schema(("summary", "str", "s"), ("done", "bool", "d")))]))),
        _cfg(_notdone_answer),
    )
    # Never signals done → terminates at the round ceiling, does not run forever.
    assert out["dispatch_round_count"] == MAX_DISPATCH_ROUNDS
    assert out["dispatch_batch_result"]["pattern"] == "loop_until_done"


# ── Denial paths — bounded, never silently truncated ─────────────────────────


@pytest.mark.anyio
async def test_depth_ceiling_denies_all_tasks_no_fanout() -> None:
    app = _build_harness()
    out = await app.ainvoke(
        cast(Any, _seed(_plan("fanout_and_synthesize", [_task(i) for i in range(2)]), dispatch_depth=2)),
        _cfg(),
    )
    results = out["_dispatch_results"]
    assert len(results) == 2
    assert all(r["status"] == "denied" for r in results)  # denied, not executed


@pytest.mark.anyio
async def test_over_width_raw_plan_bounds_denial_envelopes() -> None:
    # A raw plan far wider than the Pydantic cap: it fails validation, and the gate must
    # materialize at most MAX_DISPATCH_WIDTH denial envelopes — never one per raw task.
    app = _build_harness()
    huge = _plan("fanout_and_synthesize", [_task(i) for i in range(MAX_DISPATCH_WIDTH * 4)])
    out = await app.ainvoke(cast(Any, _seed(huge)), _cfg())
    results = out["_dispatch_results"]
    assert 0 < len(results) <= MAX_DISPATCH_WIDTH
    assert all(r["status"] == "denied" for r in results)


@pytest.mark.anyio
async def test_budget_exhaustion_denies_without_fanout() -> None:
    app = _build_harness()
    out = await app.ainvoke(
        cast(Any, _seed(_plan("fanout_and_synthesize", [_task(0)]), max_budget_usd=1e-12, current_cost_usd=0.0)),
        _cfg(),
    )
    results = out["_dispatch_results"]
    assert results and all(r["status"] == "budget_exhausted" for r in results)


# ── Result isolation: two dispatches in one run don't cross-contaminate ───────


@pytest.mark.anyio
async def test_watermark_isolates_a_second_dispatch() -> None:
    app = _build_harness()
    seed = _seed(_plan("fanout_and_synthesize", [_task(i) for i in range(2)]))
    first = await app.ainvoke(cast(Any, seed), _cfg())
    assert len(first["dispatch_batch_result"]["results"]) == 2
    consumed = first["_dispatch_consumed"]
    assert consumed == 2

    # Carry the watermark + accumulated results forward, open a fresh 1-task dispatch.
    second_seed = {
        "dispatch_plan": _plan("fanout_and_synthesize", [_task(9)]),
        "session_permission_mode": "READ_ONLY",
        "_dispatch_results": list(first["_dispatch_results"]),
        "_dispatch_consumed": consumed,
    }
    second = await app.ainvoke(cast(Any, second_seed), _cfg())
    # The second batch digests ONLY its own single envelope, not the first's two.
    assert len(second["dispatch_batch_result"]["results"]) == 1
    assert second["dispatch_batch_result"]["results"][0]["task_id"] == "t9"


# ── Permission floor-lock: analyst_readonly can never reach WRITE/EXECUTE ─────


def test_analyst_readonly_floor_locked_under_every_session_mode() -> None:
    from core.permissions import (
        PermissionDecision,
        SessionPermissionMode,
        ToolPrivilegeTier,
        evaluate_action,
    )
    from shared.rbac import resolve_dispatch_permission

    critic = resolve_dispatch_permission("analyst_readonly")
    for mode in SessionPermissionMode:
        for tier in (ToolPrivilegeTier.WRITE, ToolPrivilegeTier.EXECUTE, ToolPrivilegeTier.DANGEROUS):
            assert evaluate_action(mode, tier, critic) == PermissionDecision.DENY


def test_dev_role_resolves_write_execute_identity() -> None:
    from shared.rbac import PermissionMode, resolve_dispatch_permission

    assert resolve_dispatch_permission("core_dev") == PermissionMode.EDIT_EXECUTE_RBW
    assert resolve_dispatch_permission("analyst_readonly") == PermissionMode.READ_ONLY
    assert resolve_dispatch_permission("unknown_role") == PermissionMode.READ_ONLY  # fail-safe floor


# ── Feature-flag topology identity (R4) ──────────────────────────────────────

_DISPATCH_NODES = {
    "dispatch_origin", "dispatch_fanout", "subagent_worker",
    "dispatch_gate", "dispatch_advance", "dispatch_synthesize",
}


def _compiled_nodes(flag: str) -> set:
    """Rebuild the engine graph under a patched flag. shared.config caches the flag at
    import, so it must be reloaded before brain.engine re-reads it (R-G)."""
    import shared.config
    with mock.patch.dict(os.environ, {"AILIENANT_ENABLE_DYNAMIC_DISPATCH": flag}):
        importlib.reload(shared.config)
        import brain.engine
        importlib.reload(brain.engine)
        return set(brain.engine.alienant_app.get_graph().nodes)


def test_flag_off_graph_has_no_dispatch_nodes() -> None:
    try:
        nodes = _compiled_nodes("0")
        assert not (_DISPATCH_NODES & nodes)
    finally:
        _compiled_nodes("0")  # restore the process-default (off) graph


def test_flag_on_graph_wires_dispatch_nodes() -> None:
    try:
        nodes = _compiled_nodes("1")
        assert _DISPATCH_NODES <= nodes
    finally:
        _compiled_nodes("0")  # restore the process-default (off) graph
