# brain/daemon.py
"""
Phase 3.4.3a — OvernightDaemon stub.

Provides the START/STOP API contract; Phase 3.4.3b will fill in the real MCTS
rollout loop (selection -> expansion -> Nightmare reward -> backpropagation).
For now, _loop() is a heartbeat-only no-op so the lifecycle plumbing can be
tested in isolation. Pattern mirrors core/db_maintenance.py's WALCheckpointer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

from brain.episodic.checkpointing import MCTSCheckpointer
from brain.mcts.tree import MCTSTree

logger = logging.getLogger("OVERNIGHT_DAEMON")

_HEARTBEAT_INTERVAL_S: float = 60.0
_STOP_TIMEOUT_S: float = 5.0


class OvernightDaemon:
    """Long-running asyncio.Task that will drive MCTS rollouts during idle time."""

    def __init__(
        self,
        tree: MCTSTree,
        checkpointer: MCTSCheckpointer,
    ) -> None:
        self._tree: MCTSTree = tree
        self._checkpointer: MCTSCheckpointer = checkpointer
        self._running: bool = False
        self._task: Optional["asyncio.Task[None]"] = None
        self._background_tasks: Set["asyncio.Task[None]"] = set()

    def start(self) -> None:
        """Schedule _loop() on the current event loop. Idempotent."""
        if self._running and self._task is not None and not self._task.done():
            logger.debug("OvernightDaemon already running; start() is a no-op.")
            return
        self._running = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._loop())
        self._background_tasks.add(self._task)
        self._task.add_done_callback(self._background_tasks.discard)
        logger.info("OvernightDaemon started.")

    async def stop(self) -> None:
        """Signal the loop to exit, cancel the task, wait up to _STOP_TIMEOUT_S."""
        self._running = False
        if self._task is None or self._task.done():
            logger.debug("OvernightDaemon already stopped.")
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.debug("OvernightDaemon stop: task cancelled/timed out (expected).")
        logger.info("OvernightDaemon stopped.")

    async def _loop(self) -> None:
        """Heartbeat-only stub. Phase 3.4.3b will replace this body."""
        logger.info("OvernightDaemon _loop entered (Phase 3.4.3a stub).")
        try:
            while self._running:
                logger.debug(
                    "OvernightDaemon heartbeat: tree=%d node(s).",
                    len(self._tree),
                )
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("OvernightDaemon _loop exited.")
