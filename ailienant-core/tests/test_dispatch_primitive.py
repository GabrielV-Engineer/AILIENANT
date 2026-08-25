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
from unittest.mock import AsyncMock

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
    # Real per-invocation cost is now metered from the tool-loop context (was a 0.0
    # placeholder) — the seed prompt alone yields a small positive spend.
    assert env["cost_usd"] > 0.0


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


# ── B6 — the Glass-Box Timeline "subagent" activity marker (13.0.9) ────────────
# Previously this node emitted nothing at all — a dispatched subagent's work
# was invisible regardless of how long its tool loop ran.


@pytest.mark.anyio
async def test_worker_emits_a_subagent_activity_marker_on_success() -> None:
    push_activity = AsyncMock()
    result = await subagent_worker(
        {"_dispatch_task": _task(0, role="core_dev").model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _answer_fn, "push_activity": push_activity}},
    )
    assert result["_dispatch_results"][0]["status"] == "ok"
    push_activity.assert_awaited_once_with("subagent", target="core_dev", metric="ok", ref="t0")


@pytest.mark.anyio
async def test_worker_emits_the_marker_even_on_a_failed_answer() -> None:
    async def _boom(task: SubagentTask, obs: List[str]) -> Dict[str, Any]:
        raise RuntimeError("synth failed")

    push_activity = AsyncMock()
    await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _boom, "push_activity": push_activity}},
    )
    push_activity.assert_awaited_once_with("subagent", target="core_dev", metric="error", ref="t0")


@pytest.mark.anyio
async def test_worker_with_no_push_activity_configured_is_a_silent_no_op() -> None:
    # No "push_activity" key at all (the shape most existing call sites/tests
    # already use) must behave exactly as before this addition — no crash.
    result = await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _answer_fn}},
    )
    assert result["_dispatch_results"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_push_activity_reaches_a_send_dispatched_worker() -> None:
    """The seam this node actually uses (RunnableConfig.configurable) is an
    explicit parameter LangGraph threads to every node in a run — Send() fans
    out STATE, never config — so this is real evidence the marker fires
    through a genuine Send() dispatch, not just a direct subagent_worker(...)
    call. (A separate, narrower concern — whether core/activity_context.py's
    contextvars-based sink for record_execution's "command" markers survives
    THIS SAME fan-out — is orthogonal: that sink is bound by the turn-level
    caller in core/task_service.py, several layers above dispatch_origin, and
    is not exercised by this harness at all.)"""
    g: StateGraph = StateGraph(AIlienantGraphState)
    g.add_node("dispatch_origin", cast(Any, dispatch_origin))
    g.add_node("subagent_worker", cast(Any, subagent_worker))
    g.add_edge(START, "dispatch_origin")
    g.add_conditional_edges("dispatch_origin", dispatch_router, ["subagent_worker"])
    g.add_edge("subagent_worker", END)
    app = g.compile()

    push_activity = AsyncMock()
    result = await app.ainvoke(
        cast(AIlienantGraphState, _base_state(_plan(2))),
        {"configurable": {"dispatch_answer_fn": _answer_fn, "push_activity": push_activity}},
    )
    assert len(result["_dispatch_results"]) == 2
    assert push_activity.await_count == 2
    fired_refs = {c.kwargs.get("ref") for c in push_activity.await_args_list}
    assert fired_refs == {"t0", "t1"}
