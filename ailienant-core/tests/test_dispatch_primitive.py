# ailienant-core/tests/test_dispatch_primitive.py
"""8.15.1 DoD — generalized Send() dispatch primitive + subagent_worker fan-in.

R6 (write the concurrency test before the node lands): two concurrent Sends must
write their result envelopes into the reducer-guarded `_dispatch_results` channel
without an INVALID_CONCURRENT_GRAPH_UPDATE. The harness compiles a minimal StateGraph
(never the production engine.py graph) and drives a pure-reasoning role (empty tool
map) with an injected answer synthesiser, so no gateway or VFS is touched.
"""
from __future__ import annotations

from typing import Any, Dict, List, cast

import pytest
from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph

from brain.dispatch import build_dispatch_sends, dispatch_origin, dispatch_router
from brain.nodes.subagent_worker_node import subagent_worker
from brain.state import AIlienantGraphState
from brain.subagent_contracts import (
    DispatchPlan,
    SubagentResponseField,
    SubagentResponseSchema,
    SubagentTask,
)


def _task(i: int, role: str = "core_dev") -> SubagentTask:
    return SubagentTask(
        task_id=f"t{i}",
        description=f"do work {i}",
        subagent_role=role,  # type: ignore[arg-type]
        response_schema=SubagentResponseSchema(
            fields=[SubagentResponseField(name="summary", type="str", description="one line")]
        ),
    )


def _plan(n: int, pattern: str = "fanout_and_synthesize") -> DispatchPlan:
    return DispatchPlan(
        pattern=pattern,  # type: ignore[arg-type]
        tasks=[_task(i) for i in range(n)],
        synthesis_instruction="merge the results",
    )


async def _answer_fn(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    return {"summary": f"done {task.task_id}"}


def _base_state(plan: DispatchPlan) -> Dict[str, Any]:
    return {"dispatch_plan": plan.model_dump(), "session_permission_mode": "READ_ONLY"}


# ── B1 — build_dispatch_sends: one Send per task, dispatch_depth incremented ──


def test_build_dispatch_sends_one_per_task_with_incremented_depth() -> None:
    sends = build_dispatch_sends(_plan(3), {"dispatch_depth": 0, "dispatch_wave_count": 0})
    assert len(sends) == 3
    for s in sends:
        assert isinstance(s, Send)
        assert s.node == "subagent_worker"
        assert s.arg["dispatch_depth"] == 1
        assert "_dispatch_task" in s.arg


# ── B2 — _dispatch_results carries the operator.add reducer (the crash guard) ──


def test_dispatch_results_channel_is_reducer_annotated() -> None:
    import operator
    from typing import Annotated, get_args, get_origin, get_type_hints

    hints = get_type_hints(AIlienantGraphState, include_extras=True)
    ann = hints["_dispatch_results"]
    assert get_origin(ann) is Annotated
    assert operator.add in get_args(ann)


# ── B3 — two concurrent Sends both write; no INVALID_CONCURRENT_GRAPH_UPDATE ──


@pytest.mark.anyio
async def test_two_concurrent_sends_write_both_results() -> None:
    g: StateGraph = StateGraph(AIlienantGraphState)
    # cast(Any): production nodes take `state: Dict[str, Any]`, narrower than
    # LangGraph's TypedDict-bound NodeInputT; engine.py hides this behind node
    # decorators, the harness casts at the seam.
    g.add_node("dispatch_origin", cast(Any, dispatch_origin))
    g.add_node("subagent_worker", cast(Any, subagent_worker))
    g.add_edge(START, "dispatch_origin")
    g.add_conditional_edges("dispatch_origin", dispatch_router, ["subagent_worker"])
    g.add_edge("subagent_worker", END)
    app = g.compile()

    result = await app.ainvoke(
        cast(AIlienantGraphState, _base_state(_plan(2))),
        {"configurable": {"dispatch_answer_fn": _answer_fn}},
    )
    envelopes = result["_dispatch_results"]
    assert len(envelopes) == 2
    assert {e["status"] for e in envelopes} == {"ok"}
    assert {e["task_id"] for e in envelopes} == {"t0", "t1"}


# ── B4 — a failing answer synthesiser degrades to a status="error" envelope ──


@pytest.mark.anyio
async def test_worker_reports_error_and_never_raises() -> None:
    async def _boom(task: SubagentTask, obs: List[str]) -> Dict[str, Any]:
        raise RuntimeError("synth failed")

    result = await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _boom}},
    )
    env = result["_dispatch_results"][0]
    assert env["status"] == "error"
    assert env["structured_result"] is None


# ── B5 — response_schema validation: accept conforming, flag nonconforming ──


@pytest.mark.anyio
async def test_worker_accepts_conforming_answer() -> None:
    result = await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _answer_fn}},
    )
    env = result["_dispatch_results"][0]
    assert env["status"] == "ok"
    assert env["structured_result"] == {"summary": "done t0"}


@pytest.mark.anyio
async def test_worker_flags_nonconforming_answer() -> None:
    async def _wrong(task: SubagentTask, obs: List[str]) -> Dict[str, Any]:
        return {"not_summary": 1}

    result = await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _wrong}},
    )
    env = result["_dispatch_results"][0]
    assert env["status"] == "error"
    assert "summary" in (env["error_message"] or "")
