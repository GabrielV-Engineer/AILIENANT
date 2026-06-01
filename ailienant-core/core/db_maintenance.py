# ailienant-core/core/db_maintenance.py

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING, Optional

from brain.retry_policy import WAL_CHECKPOINT_MAX_RETRIES

if TYPE_CHECKING:
    from brain.checkpoint import HybridCheckpointer

logger = logging.getLogger("WAL_CHECKPOINTER")


class WALCheckpointer:
    """
    Asyncio background worker that keeps the SQLite WAL file from growing unbounded.

    Runs PRAGMA wal_checkpoint(TRUNCATE) every `interval_s` seconds (default 5 min).
    Defers the checkpoint if the graph is in an active write (CheckpointManager.is_writing).
    Retries on sqlite3.OperationalError with exponential backoff starting at 30s.

    Lifecycle:
        start()         → spawns asyncio.Task (call after checkpoint_manager.initialize())
        stop()          → cancels the task gracefully
        force_truncate()→ immediate checkpoint; call from lifespan shutdown before close()
    """

    def __init__(
        self, mgr: "HybridCheckpointer", interval_s: float = 300.0
    ) -> None:
        self._mgr = mgr
        self._interval = interval_s
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="wal_checkpoint")
        logger.info("WAL maintenance worker started (interval=%ds).", int(self._interval))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WAL maintenance worker stopped.")

    async def force_truncate(self) -> None:
        """Immediate checkpoint — call from lifespan shutdown before connection close."""
        conn = self._mgr.conn
        if conn is None:
            return
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
            logger.info("WAL force-truncated on shutdown.")
        except sqlite3.OperationalError as exc:
            logger.warning("WAL force-truncate failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._mgr.is_writing:
                logger.debug("WAL checkpoint deferred — active write in progress.")
                await asyncio.sleep(30.0)
                continue
            await self._checkpoint_with_backoff()

    async def _checkpoint_with_backoff(self, max_retries: int = WAL_CHECKPOINT_MAX_RETRIES) -> None:
        delay = 30.0
        for attempt in range(1, max_retries + 1):
            conn = self._mgr.conn
            if conn is None:
                return
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.commit()
                logger.info("WAL checkpoint OK (attempt %d).", attempt)
                return
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "WAL checkpoint failed attempt %d: %s — retry in %ds",
                    attempt,
                    exc,
                    int(delay),
                )
                await asyncio.sleep(delay)
                delay *= 2
