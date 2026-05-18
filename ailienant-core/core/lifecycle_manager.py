"""Phase 4.4 — Workspace-scoped lifecycle manager.

Tracks asyncio.Task objects per VS Code window PID. On workspace disconnect,
cancels all registered tasks and frees resources. Orthogonal to the
process-scoped WAL graceful shutdown from Phase 2.5/2.15.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Dict, List

logger = logging.getLogger("LIFECYCLE_MANAGER")

WORKSPACE_IDLE_SEC: int = 300  # Blueprint §4.1 — idle-timeout scheduler wired in Phase 4.5.


class WorkspaceLifecycleManager:
    def __init__(self) -> None:
        self._tasks: Dict[int, List["asyncio.Task[object]"]] = {}
        self._active: Dict[int, bool] = {}

    def register_task(self, workspace_pid: int, task: "asyncio.Task[object]") -> None:
        """Register an asyncio.Task under a workspace PID."""
        self._tasks.setdefault(workspace_pid, []).append(task)
        self._active[workspace_pid] = True
        logger.debug("Registered task for PID=%d", workspace_pid)

    def mark_inactive(self, workspace_pid: int) -> None:
        """Flag workspace inactive without cancelling tasks."""
        self._active[workspace_pid] = False

    async def shutdown_workspace(self, workspace_pid: int) -> None:
        """Cancel all tasks under workspace_pid and release resources.

        Safe to call for unknown PIDs (no-op). Workspace-scoped only —
        does not affect the server-level WAL shutdown (Phase 2.15).
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
            "Workspace PID=%d shutdown complete (%d tasks cancelled)",
            workspace_pid,
            len(tasks),
        )
        await self._release_vram()
        await self._checkpoint_wal()

    async def _release_vram(self) -> None:
        """Stub: signal Ollama keep_alive=0. Wired to LLMGateway in Phase 4.5.

        IMPORTANT (Phase 4.5 implementer): before issuing the actual HTTP call,
        add a debounce/grace timer (>=10 s) so a brief network outage that triggers
        disconnect->reconnect does NOT purge live VRAM state mid-session.
        """
        logger.info("VRAM release stub triggered.")

    async def _checkpoint_wal(self) -> None:
        """Issue WAL checkpoint if checkpoint_manager exposes the method."""
        try:
            from brain.checkpoint import checkpoint_manager  # noqa: PLC0415
            if hasattr(checkpoint_manager, "wal_checkpoint"):
                await checkpoint_manager.wal_checkpoint()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WAL checkpoint on workspace shutdown failed: %s", exc)


lifecycle_manager = WorkspaceLifecycleManager()
