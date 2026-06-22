"""Checkpoint gate for native LangGraph Suspend & Resume HITL (DEBT-070).

Proves the substrate end-to-end on a tiny one-node graph with a real checkpointer:
a node that calls ``request_graph_approval`` pauses the run (``astream`` ends
naturally; ``aget_state`` shows a pending interrupt — no exception bubbles), and a
``Command(resume=…)`` returns the decision into the node so it completes.

Node-level conversions (FinOps single-node, DriftMonitor compute→gate split, the
agentic-cell defer→exec-phase boundary) are covered in test_finops.py,
test_drift_monitor.py, and test_phase8_10_11_checkpoint_gate.py respectively.
"""
from __future__ import annotations

from typing import Any, Dict, TypedDict
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from core.hitl import extract_pending_interrupt, request_graph_approval


class _S(TypedDict):
    result: str


def _gate_node(state: _S) -> Dict[str, Any]:
    resp = request_graph_approval(
        session_id="t",
        action_description="Approve the test action?",
        proposed_content="payload",
        request_kind="TEST_GATE",
    )
    return {"result": "approved" if resp["approved"] else "rejected"}


def _compile_gate_graph() -> Any:
    g: StateGraph = StateGraph(_S)
    g.add_node("gate", _gate_node)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=MemorySaver())


async def _drain(app: Any, graph_input: Any, cfg: RunnableConfig) -> None:
    async for _ in app.astream(graph_input, config=cfg, stream_mode="values"):
        pass


@pytest.mark.anyio
async def test_interrupt_pauses_then_resumes_approved() -> None:
    app = _compile_gate_graph()
    cfg: RunnableConfig = {"configurable": {"thread_id": "gate-approve"}}

    # First run: the node calls interrupt() → the run pauses. astream ends naturally
    # (no GraphInterrupt bubbles to us); the pause is visible in the graph state.
    await _drain(app, {"result": ""}, cfg)
    snap = await app.aget_state(cfg)
    assert snap.interrupts, "graph should be paused on a pending interrupt"
    assert snap.values.get("result") in ("", None), "node body must NOT have completed pre-resume"

    # Resume with approval → interrupt() returns the value, the node completes.
    await _drain(app, Command(resume={"approved": True, "comment": None}), cfg)
    final = await app.aget_state(cfg)
    assert not final.interrupts
    assert final.values["result"] == "approved"


@pytest.mark.anyio
async def test_resume_rejected_is_honored() -> None:
    app = _compile_gate_graph()
    cfg: RunnableConfig = {"configurable": {"thread_id": "gate-reject"}}
    await _drain(app, {"result": ""}, cfg)
    await _drain(app, Command(resume={"approved": False}), cfg)
    assert (await app.aget_state(cfg)).values["result"] == "rejected"


@pytest.mark.anyio
async def test_request_graph_approval_normalizes_bare_bool_resume() -> None:
    # A bare-bool resume (not a dict) is tolerated: truthy → approved.
    app = _compile_gate_graph()
    cfg: RunnableConfig = {"configurable": {"thread_id": "gate-bool"}}
    await _drain(app, {"result": ""}, cfg)
    await _drain(app, Command(resume=True), cfg)
    assert (await app.aget_state(cfg)).values["result"] == "approved"


@pytest.mark.anyio
async def test_extract_pending_interrupt_reads_payload() -> None:
    # extract_pending_interrupt targets the compiled engine; point it at the tiny graph
    # and confirm it surfaces the interrupt payload that request_graph_approval emitted.
    app = _compile_gate_graph()
    cfg: RunnableConfig = {"configurable": {"thread_id": "gate-extract"}}
    await _drain(app, {"result": ""}, cfg)

    with patch("brain.engine.alienant_app", app):
        payload = await extract_pending_interrupt(cfg)

    assert payload is not None
    assert payload.get("request_kind") == "TEST_GATE"
    assert payload.get("action_description") == "Approve the test action?"
    assert "approval_id" in payload  # cosmetic id present for card correlation


@pytest.mark.anyio
async def test_extract_pending_interrupt_none_when_not_paused() -> None:
    app = _compile_gate_graph()
    cfg: RunnableConfig = {"configurable": {"thread_id": "gate-done"}}
    await _drain(app, {"result": ""}, cfg)
    await _drain(app, Command(resume={"approved": True}), cfg)  # finish the run
    with patch("brain.engine.alienant_app", app):
        assert await extract_pending_interrupt(cfg) is None
