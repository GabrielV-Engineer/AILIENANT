"""
core/io_coalescer.py — I/O Event Coalescer (Debounce layer for bulk file saves).

Collects file_path → content pairs in a 500ms sliding window. When the window
expires with no new events, dispatches all pending files in one sequential batch
via the registered dispatch_fn.

Key property: if a file is saved N times within the window, only the last
content is processed (dict key deduplication).

Critical file bypass: files matching _CRITICAL_PATTERNS are dispatched
immediately by the caller (main.py), not routed through this coalescer.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("IO_COALESCER")

_DEBOUNCE_S: float = 0.5
_CRITICAL_PATTERNS = frozenset({".env", "config.py", "settings.py", "secrets.py"})


def is_critical_file(filepath: str) -> bool:
    """Return True if the file should bypass debounce and dispatch immediately."""
    name = os.path.basename(filepath)
    return name in _CRITICAL_PATTERNS or name.startswith(".env")


class IOCoalescer:
    """
    Debounce layer for file-update events dispatched to the compute pool.

    Usage (in main.py lifespan):
        io_coalescer.register_dispatch(_dispatch_indexing_and_ppr)

    Usage (in WebSocket handler):
        io_coalescer.submit(filepath, content, project_id="")
    """

    def __init__(self) -> None:
        self._pending: Dict[str, Tuple[str, str]] = {}  # filepath → (content, project_id)
        self._timer: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._dispatch_fn: Optional[Callable] = None  # type: ignore[type-arg]

    def register_dispatch(self, fn: Callable) -> None:  # type: ignore[type-arg]
        """Wire in the indexing callback. Called once from lifespan startup."""
        self._dispatch_fn = fn

    def submit(self, filepath: str, content: str, project_id: str = "") -> None:
        """Accept a file update. O(1) synchronous — safe from async WebSocket handler."""
        self._pending[filepath] = (content, project_id)
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = asyncio.create_task(
            self._flush_after_debounce(), name="io_coalescer:flush"
        )

    async def _flush_after_debounce(self) -> None:
        try:
            await asyncio.sleep(_DEBOUNCE_S)
        except asyncio.CancelledError:
            return  # superseded by a newer submit

        if not self._pending or self._dispatch_fn is None:
            return
        batch = dict(self._pending)
        self._pending.clear()
        logger.info("IOCoalescer: flushing %d file(s) as one batch", len(batch))
        for filepath, (content, project_id) in batch.items():
            try:
                await self._dispatch_fn(filepath, content, project_id)
            except Exception as exc:
                logger.warning("IOCoalescer: dispatch error for %s: %s", filepath, exc)


# Global singleton imported by main.py
io_coalescer = IOCoalescer()
