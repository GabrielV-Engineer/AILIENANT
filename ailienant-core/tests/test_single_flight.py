"""Reactive re-index single-flight coordinator (concurrency safety spine).

``SingleFlightCoordinator`` guarantees at most one in-flight run per key, with a
trailing re-run so the freshest request wins (no lost update), while distinct
keys run concurrently. These properties keep overlapping save-driven re-index
from wasting work or landing a stale write after a fresh one.

Async cases run via ``asyncio.run`` (no pytest-asyncio).
"""
import asyncio
from typing import List

from core.indexer import SingleFlightCoordinator


def test_coalesces_to_one_trailing_run_newest_wins() -> None:
    """While a key is in flight, extra calls coalesce to exactly one trailing
    run, and the newest factory wins (the superseded one never executes)."""
    order: List[str] = []
    gate = asyncio.Event()

    async def _run() -> None:
        coord = SingleFlightCoordinator()

        async def first() -> None:
            order.append("start1")
            await gate.wait()
            order.append("end1")

        async def superseded() -> None:
            order.append("superseded")

        async def newest() -> None:
            order.append("newest")

        t1 = asyncio.create_task(coord.run("k", lambda: first()))
        await asyncio.sleep(0.01)          # let `first` reach the gate
        await coord.run("k", lambda: superseded())  # coalesced — returns now
        await coord.run("k", lambda: newest())      # overwrites pending
        assert order == ["start1"]         # nothing else ran while in flight
        gate.set()
        await t1                            # trailing run completes inside run()
        assert order == ["start1", "end1", "newest"]

    asyncio.run(_run())


def test_distinct_keys_run_concurrently() -> None:
    """Two different keys are not serialized against each other."""
    started: List[str] = []
    gate = asyncio.Event()

    async def _run() -> None:
        coord = SingleFlightCoordinator()

        async def block(name: str) -> None:
            started.append(name)
            await gate.wait()

        ta = asyncio.create_task(coord.run("a", lambda: block("a")))
        tb = asyncio.create_task(coord.run("b", lambda: block("b")))
        await asyncio.sleep(0.01)
        assert set(started) == {"a", "b"}  # both in flight at once
        gate.set()
        await asyncio.gather(ta, tb)

    asyncio.run(_run())


def test_sequential_calls_each_run() -> None:
    """Back-to-back calls (no overlap) each execute exactly once."""
    runs: List[int] = []

    async def _run() -> None:
        coord = SingleFlightCoordinator()

        async def work(n: int) -> None:
            runs.append(n)

        await coord.run("k", lambda: work(1))
        await coord.run("k", lambda: work(2))
        assert runs == [1, 2]

    asyncio.run(_run())
