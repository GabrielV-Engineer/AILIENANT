# tests/test_session_admission.py
"""DEBT-170 — per-session admission guard.

TaskService.is_session_busy is the single source of truth submit_task consults
before spawning a runner (a live runner or an unabandoned HITL pause both mean
"busy"); it must never mistake a finished-but-still-registered task, or an
abandoned pause past its TTL, for busy — and it must never disturb
register_active_task's own replace semantics, which test_agentic_cell_lifecycle.py
depends on separately.
"""
from __future__ import annotations

import asyncio

import pytest

import shared.config as config_module
from core import task_service as task_service_module
from core.task_service import TaskPayload, TaskService

pytestmark = pytest.mark.anyio


def _payload() -> TaskPayload:
    return TaskPayload(task_prompt="q", dirty_buffers=[])


async def test_idle_session_is_not_busy() -> None:
    ts = TaskService()
    assert ts.is_session_busy("sess-idle") is False


async def test_live_runner_is_busy() -> None:
    ts = TaskService()
    started = asyncio.Event()

    async def _slow_runner() -> None:
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_slow_runner())
    await started.wait()
    ts.register_active_task("sess-live", task)

    assert ts.is_session_busy("sess-live") is True

    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)


async def test_done_task_still_registered_is_not_busy() -> None:
    """A momentary window between task completion and its done-callback popping
    the registry entry must not read as busy — is_session_busy checks
    task.done() directly rather than mere key presence."""
    ts = TaskService()

    async def _instant() -> None:
        return

    task = asyncio.create_task(_instant())
    await task  # already done, but never registered via register_active_task —
    # simulate the registry-entry-survives-completion window directly.
    ts._active_tasks["sess-finished"] = task

    assert ts.is_session_busy("sess-finished") is False


async def test_fresh_pause_is_busy() -> None:
    ts = TaskService()
    import time

    ts._paused_tasks["sess-paused"] = (_payload(), "SEQUENTIAL", time.monotonic())

    assert ts.is_session_busy("sess-paused") is True
    assert ts.has_paused_graph("sess-paused") is True


async def test_abandoned_pause_past_ttl_is_not_busy_and_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "PAUSED_INTERRUPT_TTL_S", 0.01)
    monkeypatch.setattr(task_service_module, "PAUSED_INTERRUPT_TTL_S", 0.01)

    ts = TaskService()
    import time

    ts._paused_tasks["sess-abandoned"] = (
        _payload(), "SEQUENTIAL", time.monotonic() - 10.0,
    )

    assert ts.is_session_busy("sess-abandoned") is False
    # Reclaimed, not merely reported free — a late reply resolves via
    # resume_graph's existing no-such-entry no-op, never a stale re-resume.
    assert "sess-abandoned" not in ts._paused_tasks


async def test_register_active_task_replace_semantics_unaffected() -> None:
    """is_session_busy must not change register_active_task's documented
    idempotent-replace behavior — test_agentic_cell_lifecycle.py's successor-task
    guard depends on a second registration for the same session_id replacing the
    first, not being rejected."""
    ts = TaskService()
    started_a = asyncio.Event()
    started_b = asyncio.Event()

    async def _runner(started: asyncio.Event) -> None:
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return

    task_a = asyncio.create_task(_runner(started_a))
    await started_a.wait()
    ts.register_active_task("sess-succ", task_a)
    assert ts._active_tasks["sess-succ"] is task_a

    task_b = asyncio.create_task(_runner(started_b))
    await started_b.wait()
    ts.register_active_task("sess-succ", task_b)
    assert ts._active_tasks["sess-succ"] is task_b

    task_a.cancel()
    task_b.cancel()
    await asyncio.wait_for(
        asyncio.gather(task_a, task_b, return_exceptions=True), timeout=1.0
    )
