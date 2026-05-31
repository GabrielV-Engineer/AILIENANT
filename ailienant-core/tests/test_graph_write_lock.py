"""Per-project graph-write serialization (concurrency safety spine).

Under the event-driven Push model the dependency graph gains several concurrent
writers (reactive save re-index, memory consolidation, agent apply_patch), each
running a non-atomic DELETE-then-INSERT. ``core.db.graph_write_lock`` serializes
them per project so no writer observes — or produces — a torn graph.

Async cases run via ``asyncio.run`` (no pytest-asyncio); the catalog DB is
isolated per-test onto ``tmp_path`` via the ``DB_CATALOG_PATH`` seam.
"""
import asyncio
from typing import Any, List

import pytest

from core import db as catalog_db


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def test_lock_identity_per_project(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same project → same lock instance; distinct projects never contend."""
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        same_a = catalog_db.graph_write_lock("projA")
        same_b = catalog_db.graph_write_lock("projA")
        other = catalog_db.graph_write_lock("projB")
        assert same_a is same_b
        assert same_a is not other

    asyncio.run(_run())


def test_writer_blocks_while_lock_held(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph write cannot land while the per-project lock is held elsewhere."""
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        lock = catalog_db.graph_write_lock("proj")
        await lock.acquire()
        # The writer acquires the SAME cached lock internally, so it must block.
        writer = asyncio.create_task(
            catalog_db.upsert_dependencies("a.py", ["b.py"], "proj")
        )
        await asyncio.sleep(0.05)  # let the writer reach its blocked acquire
        assert await catalog_db.get_all_edges("proj") == []  # nothing written yet
        lock.release()
        await writer
        assert ("a.py", "b.py") in await catalog_db.get_all_edges("proj")

    asyncio.run(_run())


def test_concurrent_upserts_never_tear(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent DELETE+INSERT for one file resolves to exactly one coherent set.

    Without serialization, one writer's INSERT could interleave with another's
    DELETE, leaving a mixed/torn edge set. The lock guarantees the survivor is
    exactly one of the input sets — never a blend.
    """
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()

        async def write(imports: List[str]) -> None:
            await catalog_db.upsert_dependencies("a.py", imports, "proj")

        await asyncio.gather(
            write(["x1.py", "x2.py", "x3.py"]),
            write(["y1.py", "y2.py"]),
            write(["z1.py"]),
        )
        edges = {t for _s, t in await catalog_db.get_all_edges("proj")}
        valid = (
            {"x1.py", "x2.py", "x3.py"},
            {"y1.py", "y2.py"},
            {"z1.py"},
        )
        assert edges in valid

    asyncio.run(_run())
