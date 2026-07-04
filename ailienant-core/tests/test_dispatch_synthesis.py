# ailienant-core/tests/test_dispatch_synthesis.py
"""8.15.2 DoD — dispatch synthesis + sequential wave batching.

Covers: wave-slicing by dispatch_wave_count; the bounded loop-back decision on the
fan-in gate; the per-batch digest ceiling keyed off the parent's active_llm_profile;
and an end-to-end compiled harness proving a plan wider than the concurrency cap runs
in bounded sequential waves and folds into one DispatchBatchResult (no runaway fan-out).
"""
from __future__ import annotations

from typing import Any, Dict, List, cast

import pytest
from langgraph.graph import END, START, StateGraph

from brain.dispatch import (
    build_dispatch_sends,
    dispatch_gate,
    dispatch_origin,
    dispatch_router,
    route_after_workers,
)
from brain.nodes.dispatch_synthesize_node import dispatch_synthesize
from brain.nodes.subagent_worker_node import subagent_worker
from brain.state import AIlienantGraphState, LLMProfile
from brain.subagent_contracts import (
    DispatchPlan,
    SubagentResponseField,
    SubagentResponseSchema,
    SubagentResultEnvelope,
    SubagentTask,
)


def _task(i: int) -> SubagentTask:
    return SubagentTask(
        task_id=f"t{i}",
        description=f"do work {i}",
        subagent_role="core_dev",
        response_schema=SubagentResponseSchema(
            fields=[SubagentResponseField(name="summary", type="str", description="one line")]
        ),
    )


def _plan(n: int, pattern: str = "fanout_and_synthesize") -> DispatchPlan:
    return DispatchPlan(
        pattern=pattern,  # type: ignore[arg-type]
        tasks=[_task(i) for i in range(n)],
        synthesis_instruction="merge",
    )


async def _answer_fn(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    return {"summary": f"done {task.task_id}"}


# ── W1 — build_dispatch_sends slices tasks by wave ──────────────────────────


def test_build_dispatch_sends_slices_by_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.dispatch.MAX_CONCURRENT_SUBAGENTS", 4)
    plan = _plan(10)
    w0 = build_dispatch_sends(plan, {"dispatch_wave_count": 0})
    w1 = build_dispatch_sends(plan, {"dispatch_wave_count": 1})
    w2 = build_dispatch_sends(plan, {"dispatch_wave_count": 2})
    assert [len(w0), len(w1), len(w2)] == [4, 4, 2]
    assert w2[0].arg["_dispatch_task"]["task_id"] == "t8"


# ── W2 — route_after_workers loops until the plan is spent, then synthesizes ──


def test_route_after_workers_loops_then_synthesizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.dispatch.MAX_CONCURRENT_SUBAGENTS", 4)
    plan_dump = _plan(10).model_dump()
    assert route_after_workers({"dispatch_wave_count": 1, "dispatch_plan": plan_dump}) == "dispatch_origin"
    assert route_after_workers({"dispatch_wave_count": 2, "dispatch_plan": plan_dump}) == "dispatch_origin"
    assert route_after_workers({"dispatch_wave_count": 3, "dispatch_plan": plan_dump}) == "dispatch_synthesize"


def test_dispatch_gate_advances_wave_count() -> None:
    assert dispatch_gate({"dispatch_wave_count": 0}) == {"dispatch_wave_count": 1}
    assert dispatch_gate({}) == {"dispatch_wave_count": 1}


# ── W3 — the digest ceiling drops whole over-budget envelopes ────────────────


@pytest.mark.anyio
async def test_synthesize_respects_active_profile_digest_ceiling() -> None:
    envs = [
        SubagentResultEnvelope(
            task_id=f"t{i}", status="ok", raw_digest="x" * 4000, cost_usd=0.5
        ).model_dump()
        for i in range(5)
    ]
    # ceiling = 1000 * 4.0 * 0.5 = 2000 chars; each digest is 4000 → only the first fits.
    state: Dict[str, Any] = {
        "_dispatch_results": envs,
        "dispatch_plan": _plan(5).model_dump(),
        "active_llm_profile": LLMProfile(
            model_name="m", parameters_b=1.0, context_window=1000, quantization="q"
        ),
    }
    result = await dispatch_synthesize(state)
    batch = result["dispatch_batch_result"]
    assert len(batch["results"]) < 5
    assert batch["total_cost_usd"] == pytest.approx(2.5)  # cost of ALL 5, even those trimmed
    assert batch["winner_task_id"] is None
    assert batch["pattern"] == "fanout_and_synthesize"


# ── W4 — end-to-end: 6 tasks / cap 4 → 2 sequential waves → one batch of 6 ───


@pytest.mark.anyio
async def test_full_dispatch_cycle_runs_bounded_waves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.dispatch.MAX_CONCURRENT_SUBAGENTS", 4)
    g: StateGraph = StateGraph(AIlienantGraphState)
    # cast(Any): production nodes take `state: Dict[str, Any]`, narrower than
    # LangGraph's TypedDict-bound NodeInputT; the harness casts at the seam.
    g.add_node("dispatch_origin", cast(Any, dispatch_origin))
    g.add_node("subagent_worker", cast(Any, subagent_worker))
    g.add_node("dispatch_gate", cast(Any, dispatch_gate))
    g.add_node("dispatch_synthesize", cast(Any, dispatch_synthesize))
    g.add_edge(START, "dispatch_origin")
    g.add_conditional_edges("dispatch_origin", dispatch_router, ["subagent_worker"])
    g.add_edge("subagent_worker", "dispatch_gate")
    g.add_conditional_edges(
        "dispatch_gate", route_after_workers,
        {"dispatch_origin": "dispatch_origin", "dispatch_synthesize": "dispatch_synthesize"},
    )
    g.add_edge("dispatch_synthesize", END)
    app = g.compile()

    state = {"dispatch_plan": _plan(6).model_dump(), "session_permission_mode": "READ_ONLY"}
    result = await app.ainvoke(
        cast(AIlienantGraphState, state),
        {"configurable": {"dispatch_answer_fn": _answer_fn}},
    )
    assert result["dispatch_wave_count"] == 2                    # 4 + 2, two waves
    assert len(result["_dispatch_results"]) == 6                 # every worker wrote once
    batch = result["dispatch_batch_result"]
    assert len(batch["results"]) == 6                            # all fit under the default window
    assert batch["winner_task_id"] is None
