"""Phase 4.4/4.5 — Workspace-scoped lifecycle manager.

Phase 4.4: Tracks asyncio.Task objects per VS Code window PID; cancels them on
disconnect. Orthogonal to the process-scoped WAL graceful shutdown from
Phase 2.5/2.15.

Phase 4.5: Adds a debounce timer in front of VRAM purge to survive brief
network outages (disconnect -> reconnect within debounce_sec must NOT release
KV cache). Adds release_vram_on_mode_switch() as a separate immediate-fire
path for inter-run mode transitions (no debounce — modes don't bounce).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Dict, List

logger = logging.getLogger("LIFECYCLE_MANAGER")

WORKSPACE_IDLE_SEC: int = 300  # Blueprint §4.1 — idle-timeout scheduler wired in Phase 5.
DEFAULT_DEBOUNCE_SEC: float = 10.0  # Phase 4.5 — survive brief network blips.


class WorkspaceLifecycleManager:
    def __init__(self, debounce_sec: float = DEFAULT_DEBOUNCE_SEC) -> None:
        self._tasks: Dict[int, List["asyncio.Task[object]"]] = {}
        self._active: Dict[int, bool] = {}
        self._debounce_sec: float = debounce_sec
        # Pending VRAM purges keyed by PID. Cancelled if a new task registers
        # for the same PID before the timer fires (phantom-reconnect guard).
        self._pending_purges: Dict[int, asyncio.TimerHandle] = {}

    def register_task(self, workspace_pid: int, task: "asyncio.Task[object]") -> None:
        """Register an asyncio.Task under a workspace PID.

        Side effect: cancels any pending VRAM purge for this PID so a brief
        WebSocket disconnect followed by an immediate reconnect does NOT
        evict the live KV cache mid-session.
        """
        pending = self._pending_purges.pop(workspace_pid, None)
        if pending is not None:
            pending.cancel()
            logger.info(
                "Cancelled pending VRAM purge for PID=%d (reconnect within debounce).",
                workspace_pid,
            )
        self._tasks.setdefault(workspace_pid, []).append(task)
        self._active[workspace_pid] = True
        logger.debug("Registered task for PID=%d", workspace_pid)

    def mark_inactive(self, workspace_pid: int) -> None:
        """Flag workspace inactive without cancelling tasks."""
        self._active[workspace_pid] = False

    async def shutdown_workspace(self, workspace_pid: int) -> None:
        """Cancel all tasks under workspace_pid immediately; schedule the VRAM
        purge to fire after the debounce window so phantom reconnects can veto
        it. Workspace-scoped — does not affect the server-level WAL shutdown.
        Safe to call for unknown PIDs (no-op).
        """
        self._active.pop(workspace_pid, None)
        # .pop() before iteration is intentional: unlinks the list from the dict
        # before any awaits, eliminating the race condition where a completing
        # task might concurrently try to modify self._tasks mid-loop.
        tasks = self._tasks.pop(workspace_pid, [])
        for task in tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        logger.info(
            "Workspace PID=%d shutdown — %d tasks cancelled, VRAM purge scheduled in %.2fs.",
            workspace_pid,
            len(tasks),
            self._debounce_sec,
        )
        await self._checkpoint_wal()
        self._schedule_vram_purge(workspace_pid)

    def _schedule_vram_purge(self, workspace_pid: int) -> None:
        """Arm a TimerHandle that fires _release_vram after debounce_sec."""
        loop = asyncio.get_running_loop()
        existing = self._pending_purges.pop(workspace_pid, None)
        if existing is not None:
            existing.cancel()
        handle = loop.call_later(
            self._debounce_sec,
            lambda: asyncio.create_task(self._fire_purge(workspace_pid)),
        )
        self._pending_purges[workspace_pid] = handle

    async def _fire_purge(self, workspace_pid: int) -> None:
        """Timer callback: invoked only if the debounce window elapsed without
        a reconnect (register_task would have cancelled the TimerHandle).
        """
        self._pending_purges.pop(workspace_pid, None)
        await self._release_vram()

    async def _release_vram(self) -> None:
        """Stub: signal Ollama keep_alive=0. Real HTTP call wired in Phase 5."""
        logger.info("VRAM release fired (post-debounce).")

    async def release_vram_on_mode_switch(self) -> None:
        """Immediate (no debounce) VRAM release for inter-run execution_mode
        changes. Different concern from disconnect: modes don't bounce, KV
        cache from a prior SEQUENTIAL run is stale for FULL_SWARM (different
        system prompts / role definitions). Real HTTP call wired in Phase 5.
        """
        logger.info("VRAM release fired (mode switch).")

    async def _checkpoint_wal(self) -> None:
        """Issue WAL checkpoint if checkpoint_manager exposes the method."""
        try:
            from brain.checkpoint import checkpoint_manager  # noqa: PLC0415
            if hasattr(checkpoint_manager, "wal_checkpoint"):
                await checkpoint_manager.wal_checkpoint()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WAL checkpoint on workspace shutdown failed: %s", exc)


lifecycle_manager = WorkspaceLifecycleManager()
