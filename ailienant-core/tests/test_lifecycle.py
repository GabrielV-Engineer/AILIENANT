"""Phase 4.4 — WorkspaceLifecycleManager unit tests."""
import asyncio
import pytest
from core.lifecycle_manager import WorkspaceLifecycleManager

pytestmark = pytest.mark.anyio


async def _blocking(event: asyncio.Event) -> None:
    await event.wait()


async def test_register_and_cancel_task() -> None:
    mgr = WorkspaceLifecycleManager()
    event = asyncio.Event()
    task = asyncio.create_task(_blocking(event))
    mgr.register_task(1234, task)
    await mgr.shutdown_workspace(1234)
    assert task.cancelled()


async def test_shutdown_unknown_pid_is_noop() -> None:
    mgr = WorkspaceLifecycleManager()
    await mgr.shutdown_workspace(99999)  # must not raise


async def test_mark_inactive_does_not_cancel() -> None:
    mgr = WorkspaceLifecycleManager()
    event = asyncio.Event()
    task = asyncio.create_task(_blocking(event))
    mgr.register_task(5678, task)
    mgr.mark_inactive(5678)
    assert not task.cancelled()
    task.cancel()


async def test_multiple_tasks_all_cancelled() -> None:
    mgr = WorkspaceLifecycleManager()
    event = asyncio.Event()
    tasks = [asyncio.create_task(_blocking(event)) for _ in range(3)]
    for t in tasks:
        mgr.register_task(7777, t)
    await mgr.shutdown_workspace(7777)
    assert all(t.cancelled() for t in tasks)
