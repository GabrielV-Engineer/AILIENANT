from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable, Optional, TypeVar

import psutil

logger = logging.getLogger("COMPUTE_POOL")
T = TypeVar("T")


class ComputePoolManager:
    """
    Async bridge between the asyncio event loop and a ProcessPoolExecutor.

    Worker count = max(1, physical_cores - 1) — one core is reserved for the
    OS and the Ollama/LiteLLM process to prevent thermal throttling.

    Lifecycle (wired to FastAPI lifespan):
        startup  → compute_pool.initialize(initializer=_worker_init)
        shutdown → compute_pool.shutdown(wait=True, cancel_futures=True)

    Zombie protection: ProcessPoolExecutor (Python 3.9+) silently replaces a
    crashed worker on the next submit(). The BrokenProcessPool handler below
    covers the rare case where the pool management thread itself dies.
    """

    def __init__(self) -> None:
        self._pool: Optional[ProcessPoolExecutor] = None
        self._initializer: Optional[Callable[[], None]] = None
        physical = psutil.cpu_count(logical=False) or 2
        self._max_workers: int = max(1, physical - 1)

    def initialize(self, initializer: Optional[Callable[[], None]] = None) -> None:
        """Start the pool. Call once from lifespan startup."""
        self._initializer = initializer
        self._pool = ProcessPoolExecutor(
            max_workers=self._max_workers,
            initializer=initializer,
        )
        logger.info("ComputePool started: %d worker(s) (physical cores - 1)", self._max_workers)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        """Stop the pool. Call from lifespan shutdown."""
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._pool = None
            logger.info("ComputePool stopped.")

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Submit fn(*args) to the process pool and await the result.

        Transparently resurrects the pool on BrokenProcessPool so transient
        worker crashes don't take down the server.
        """
        assert self._pool is not None, "ComputePoolManager.initialize() was not called"
        loop = asyncio.get_running_loop()
        try:
            result: T = await loop.run_in_executor(self._pool, fn, *args)
            return result
        except concurrent.futures.process.BrokenProcessPool:
            logger.error(
                "Process pool broken — resurrecting with %d workers", self._max_workers
            )
            self._pool = ProcessPoolExecutor(
                max_workers=self._max_workers,
                initializer=self._initializer,
            )
            result = await loop.run_in_executor(self._pool, fn, *args)
            return result

    @property
    def max_workers(self) -> int:
        return self._max_workers


# Global singleton imported by main.py
compute_pool = ComputePoolManager()
