# ailienant-core/tests/test_agentic_cell_lifecycle.py
"""DEBT-152 — Orphaned agentic-cell session teardown.

``brain.agentic_cell.sweep_orphaned_sessions`` existed with zero callers: an
aborted run's ``_CellSession`` held its container lease at ``refcount >= 1``
forever, immune to the pool's idle-TTL reaping. This suite certifies the two
wiring paths added to close it, and — more importantly — the two guards that
keep the fix from destroying a still-legitimately-open cell:

  LIFE1  a cancelled runner task's done-callback closes its registered cell.
  LIFE2  a HITL-paused session's cell survives its runner task completing
         (the ``has_paused_graph`` guard) — the pause is a native ``interrupt()``
         that ends the runner coroutine while the cell must stay alive for
         ``resume_graph`` to re-enter later.
  LIFE3  a successor task's registration for the same session_id keeps the
         PRIOR task's completion from closing the cell (the "is this session
         still claimed" guard) — e.g. an analyst query runner finishing after
         a coder turn already re-registered the session must not tear down a
         live coder-turn cell.
  LIFE4  the disconnect sweep closes a genuinely orphaned session while
         leaving a live (active) and a paused session alone.
  LIFE5  the background teardown-task strong-ref set always drains back to
         empty — the fire-and-forget scheduling must never leak Tasks.
  LIFE6  every path is idempotent (closing an already-closed / unknown
         session_id is a no-op, never an exception).
"""
from __future__ import annotations

import asyncio
import time
from typing import Iterator

import pytest

import brain.agentic_cell as ac
from core.task_service import TaskPayload, TaskService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_cell_registry() -> Iterator[None]:
    ac._session_registry.clear()
    yield
    ac._session_registry.clear()


class _FakeSession:
    """Minimal stand-in for a SandboxSession — only close() is exercised here."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _register_cell(task_id: str) -> _FakeSession:
    session = _FakeSession()
    ac._session_registry[task_id] = ac._CellSession(session=session, surface=None)
    return session


def _minimal_payload() -> TaskPayload:
    return TaskPayload(
        task_prompt="noop", project_id="proj", dirty_buffers=[], explicit_mentions=[],
    )


# ── LIFE1 — cancelled runner task closes its cell ────────────────────────────


async def test_life1_cancelled_task_closes_its_cell() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    session = _register_cell("sess-1")
    started = asyncio.Event()

    async def _runner() -> None:
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_runner())
    await started.wait()
    ts.register_active_task("sess-1", task)

    assert ts.abort_session("sess-1") is True
    await asyncio.wait_for(task, timeout=1.0)

    # The teardown is scheduled from a done-callback (fire-and-forget) — give the
    # event loop one tick to run the scheduled task before asserting.
    await asyncio.sleep(0)
    await asyncio.wait_for(_drain(ts), timeout=1.0)

    assert "sess-1" not in ac._session_registry
    assert session.closed is True


# ── LIFE2 — a HITL-paused session's cell survives its runner completing ──────


async def test_life2_paused_graph_guard_preserves_the_cell() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    session = _register_cell("sess-2")

    # Simulate the native-interrupt pause: the runner completes normally (no
    # cancellation) while task_service._paused_tasks records the suspended turn —
    # exactly the sequence _run_coding_task follows around task_service.py:1076-1086.
    ts._paused_tasks["sess-2"] = (_minimal_payload(), "AUTO", time.monotonic())

    async def _runner() -> None:
        return None

    task = asyncio.create_task(_runner())
    ts.register_active_task("sess-2", task)
    await asyncio.wait_for(task, timeout=1.0)
    await asyncio.sleep(0)
    await _drain(ts)

    assert "sess-2" in ac._session_registry, "paused session's cell must survive"
    assert session.closed is False


# ── LIFE3 — a successor task's registration protects the predecessor's cell ──


async def test_life3_successor_task_protects_the_cell() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    session = _register_cell("sess-3")

    predecessor_done = asyncio.Event()

    async def _predecessor() -> None:
        await predecessor_done.wait()

    async def _successor() -> None:
        await asyncio.sleep(5.0)

    pred = asyncio.create_task(_predecessor())
    ts.register_active_task("sess-3", pred)

    succ = asyncio.create_task(_successor())
    # Register the successor BEFORE releasing the predecessor, so the predecessor's
    # done-callback observes an already-claimed registry entry — mirroring a second
    # runner (e.g. an analyst query) starting for the same session before the first
    # one's completion is processed by the event loop.
    ts.register_active_task("sess-3", succ)

    predecessor_done.set()
    await asyncio.wait_for(pred, timeout=1.0)
    await asyncio.sleep(0)
    await _drain(ts)

    assert "sess-3" in ac._session_registry, "successor's cell must not be closed"
    assert session.closed is False

    succ.cancel()
    with pytest.raises(asyncio.CancelledError):
        await succ


# ── LIFE4 — disconnect sweep reaps only the genuine orphan ───────────────────


async def test_life4_disconnect_sweep_reaps_only_the_orphan() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    orphan_session = _register_cell("sess-orphan")
    live_session = _register_cell("sess-live")
    paused_session = _register_cell("sess-paused")

    live_task = asyncio.create_task(asyncio.sleep(5.0))
    ts.register_active_task("sess-live", live_task)
    ts._paused_tasks["sess-paused"] = (_minimal_payload(), "AUTO", time.monotonic())
    # "sess-orphan" is registered in the cell registry but has no entry in either
    # _active_tasks or _paused_tasks — exactly what a task that died without
    # either lifecycle hook firing leaves behind.

    live = ts.live_task_ids()
    assert live == {"sess-live", "sess-paused"}

    swept = await ac.sweep_orphaned_sessions(live)

    assert swept == 1
    assert "sess-orphan" not in ac._session_registry
    assert orphan_session.closed is True
    assert "sess-live" in ac._session_registry
    assert live_session.closed is False
    assert "sess-paused" in ac._session_registry
    assert paused_session.closed is False

    live_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await live_task


# ── LIFE5 — the fire-and-forget strong-ref set always drains ─────────────────


async def test_life5_teardown_task_set_drains_to_empty() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    _register_cell("sess-5")

    async def _runner() -> None:
        return None

    task = asyncio.create_task(_runner())
    ts.register_active_task("sess-5", task)
    await asyncio.wait_for(task, timeout=1.0)
    await asyncio.sleep(0)
    await _drain(ts)

    assert ts._cell_teardown_tasks == set()


# ── LIFE6 — idempotency: unknown / already-closed session_id is a safe no-op ─


async def test_life6_teardown_paths_are_idempotent() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]

    # No cell registered at all for this session — must not raise.
    await ac.close_cell_session("never-existed")

    # Closing twice is safe.
    session = _register_cell("sess-6")
    await ac.close_cell_session("sess-6")
    assert session.closed is True
    await ac.close_cell_session("sess-6")  # second close: no-op, no raise

    # sweep_orphaned_sessions over an empty registry is a safe no-op.
    assert await ac.sweep_orphaned_sessions(set()) == 0

    # _schedule_cell_teardown for a session with no registered task at all.
    ts._schedule_cell_teardown("sess-never-registered")
    await asyncio.sleep(0)
    await _drain(ts)
    assert ts._cell_teardown_tasks == set()


# ── helper ────────────────────────────────────────────────────────────────────


async def _drain(ts: TaskService) -> None:
    """Await every currently in-flight fire-and-forget cell-teardown task, then
    yield once more so each task's own ``discard`` done-callback has run.

    ``_schedule_cell_teardown`` schedules via ``asyncio.create_task`` from a
    done-callback; awaiting the task itself guarantees its BODY has finished, but
    ``add_done_callback(self._cell_teardown_tasks.discard)`` is itself scheduled
    via ``loop.call_soon`` at completion — a separate callback, not synchronous
    with the awaiter waking up. One extra ``asyncio.sleep(0)`` lets that callback
    run before the set is asserted empty.
    """
    pending = list(ts._cell_teardown_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)
