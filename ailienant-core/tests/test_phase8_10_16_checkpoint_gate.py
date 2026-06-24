"""Checkpoint gate for HITL restart-durability (DEBT-072).

Proves that a native LangGraph ``interrupt()`` suspended before a process restart
survives it. A restart is simulated by promoting to L2 on one ``HybridCheckpointer``
instance and recovering on a brand-new instance bound to the same sqlite file.

Locked invariants:
  1. ``recover()`` re-seeds the pending interrupt write — the recovered checkpointer
     carries it (``get_tuple().pending_writes``) and ``aget_state`` surfaces it.
  2. The interrupt resumes across the cold instance via ``Command(resume=…)``.
  3. After a resume re-promotes the cleared head, a later recover restores the cleared
     head — never a stale, already-answered interrupt (the ``time.time()`` ordering +
     ``checkpoint_id`` tie-break fix).
  4. Multiple pending writes on one ``task_id`` round-trip (the ``write_idx`` fix).
  5. A clean (no-interrupt) run recovers with no pending writes.
  6. A checkpointed security posture (``session_permission_mode``) survives the restart —
     the exact datum the resume path seeds into the out-of-graph MCP permission gate.

Test-only; sibling-gate convention. A real ``HybridCheckpointer`` on a ``tmp_path`` file;
no network, no live MCP, no real cognitive engine.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from brain.checkpoint import HybridCheckpointer
from core.hitl import request_graph_approval

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cp_factory(tmp_path: Any) -> Any:
    """Yield a factory minting ``HybridCheckpointer`` instances on one shared db file.

    Each call returns a fresh instance on the same path — minting a second one models a
    process restart that re-opens the persisted L2 store. All instances are closed at
    teardown so Windows can drop the sqlite handles before ``tmp_path`` is removed.
    """
    db_path = str(tmp_path / "hybrid_state.sqlite")
    created: List[HybridCheckpointer] = []

    def make() -> HybridCheckpointer:
        cp = HybridCheckpointer(db_path=db_path)
        cp.initialize()
        created.append(cp)
        return cp

    yield make

    for cp in created:
        cp.close()


class _S(TypedDict, total=False):
    result: str
    session_permission_mode: str


def _gate_node(state: _S) -> Dict[str, Any]:
    resp = request_graph_approval(
        session_id="t",
        action_description="Approve the test action?",
        proposed_content="payload",
        request_kind="TEST_GATE",
    )
    return {"result": "approved" if resp["approved"] else "rejected"}


def _compile_gate(cp: HybridCheckpointer) -> Any:
    g: StateGraph = StateGraph(_S)
    g.add_node("gate", _gate_node)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=cp)


async def _drain(app: Any, graph_input: Any, cfg: RunnableConfig) -> None:
    async for _ in app.astream(graph_input, config=cfg, stream_mode="values"):
        pass


# ── Invariant 1 + 2: interrupt survives a restart and resumes ──────────

async def test_interrupt_survives_restart_and_resumes(
    cp_factory: Callable[[], HybridCheckpointer],
) -> None:
    cp1 = cp_factory()
    cfg: RunnableConfig = {"configurable": {"thread_id": "durable"}}

    await _drain(_compile_gate(cp1), {"result": ""}, cfg)  # pauses on interrupt()
    cp1.promote("durable")

    # Simulate a process restart: a brand-new checkpointer on the same db file.
    cp2 = cp_factory()
    cp2.recover("durable")
    ct = cp2.get_tuple(cfg)
    assert ct is not None and ct.pending_writes, (
        "recovered checkpointer must carry the pending interrupt write"
    )

    app2 = _compile_gate(cp2)
    snap = await app2.aget_state(cfg)
    assert snap.interrupts, "the recovered checkpointer must surface the pending interrupt"
    assert snap.values.get("result") in ("", None), "the node body must not have completed"

    # Resume across the cold instance → the interrupt genuinely survived the restart.
    await _drain(app2, Command(resume={"approved": True, "comment": None}), cfg)
    final = await app2.aget_state(cfg)
    assert not final.interrupts
    assert final.values["result"] == "approved"


# ── Invariant 3: no stale interrupt resurrection after a resume ─────────

async def test_no_stale_interrupt_resurrection_after_resume(
    cp_factory: Callable[[], HybridCheckpointer],
) -> None:
    cp1 = cp_factory()
    cfg: RunnableConfig = {"configurable": {"thread_id": "stale"}}
    await _drain(_compile_gate(cp1), {"result": ""}, cfg)
    cp1.promote("stale")  # persists the interrupt head

    cp2 = cp_factory()
    cp2.recover("stale")
    await _drain(_compile_gate(cp2), Command(resume={"approved": True}), cfg)
    cp2.promote("stale")  # persists the cleared (no-interrupt) head

    # A later reopen must restore the CLEARED head, not the older interrupt row — proves
    # wall-clock ordering + the checkpoint_id tie-break pick the genuinely-latest row.
    cp3 = cp_factory()
    cp3.recover("stale")
    ct = cp3.get_tuple(cfg)
    assert ct is not None and not ct.pending_writes
    snap = await _compile_gate(cp3).aget_state(cfg)
    assert not snap.interrupts
    assert snap.values.get("result") == "approved"


# ── Invariant 4: multiple pending writes per task_id round-trip ─────────

async def test_multiple_pending_writes_per_task_round_trip(
    cp_factory: Callable[[], HybridCheckpointer],
) -> None:
    cp1 = cp_factory()
    cfg: RunnableConfig = {"configurable": {"thread_id": "multiw", "checkpoint_ns": ""}}
    saved = cp1.put(cfg, empty_checkpoint(), {}, {})
    cp1.put_writes(saved, [("alpha", "A"), ("beta", "B")], "task-1")
    cp1.promote("multiw")

    cp2 = cp_factory()
    cp2.recover("multiw")
    ct = cp2.get_tuple({"configurable": {"thread_id": "multiw"}})
    assert ct is not None and ct.pending_writes is not None
    channels = sorted(w[1] for w in ct.pending_writes)
    assert channels == ["alpha", "beta"], (
        "both writes on one task_id must survive — a fixed write_idx would clobber one"
    )


# ── Invariant 5: clean run recovers without pending writes ─────────────

async def test_clean_run_recovers_without_pending_writes(
    cp_factory: Callable[[], HybridCheckpointer],
) -> None:
    cp1 = cp_factory()
    cfg: RunnableConfig = {"configurable": {"thread_id": "clean"}}
    app1 = _compile_gate(cp1)
    await _drain(app1, {"result": ""}, cfg)
    await _drain(app1, Command(resume={"approved": True}), cfg)  # run to completion
    cp1.promote("clean")

    cp2 = cp_factory()
    cp2.recover("clean")
    ct = cp2.get_tuple(cfg)
    assert ct is not None and not ct.pending_writes
    snap = await _compile_gate(cp2).aget_state(cfg)
    assert not snap.interrupts


# ── Invariant 6: security posture survives the restart ─────────────────

async def test_security_posture_survives_restart(
    cp_factory: Callable[[], HybridCheckpointer],
) -> None:
    cp1 = cp_factory()
    cfg: RunnableConfig = {"configurable": {"thread_id": "posture"}}
    # Seed the posture as a checkpointed channel, then pause on the gate.
    await _drain(_compile_gate(cp1), {"result": "", "session_permission_mode": "ASK_ALL"}, cfg)
    cp1.promote("posture")

    cp2 = cp_factory()
    cp2.recover("posture")
    snap = await _compile_gate(cp2).aget_state(cfg)
    # The value the resume-branch MCP gate reads back is the saved posture, not DEFAULT.
    assert snap.values.get("session_permission_mode") == "ASK_ALL"
