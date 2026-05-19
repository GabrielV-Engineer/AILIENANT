"""Phase 6.9 — Dead Letter Queue formal-delivery suite (delivers Phase 6.4).

Validates the three load-bearing guarantees of ``core/dead_letter.py``: the DLQ
table + index are created idempotently, the ``dead_letter_decorator`` intercepts
an unhandled node exception (L1→L2 promotion attempted, row persisted, exception
re-raised), and the resume lifecycle is idempotent (a resolved episode no longer
appears as pending). Async cases run via ``asyncio.run`` — no ``pytest-asyncio``
dependency. The catalog DB is isolated per-test by monkeypatching the
``DB_CATALOG_PATH`` seam onto a ``tmp_path`` file.
"""
import asyncio
import sqlite3
from typing import Any, Dict, List

import pytest

from brain.checkpoint import checkpoint_manager
from core import dead_letter
from core.dead_letter import (
    dead_letter_decorator,
    get_pending_dlqs,
    init_dlq_table,
    mark_dlq_resolved,
    save_dead_letter,
)


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point ``core.dead_letter`` at a throwaway catalog DB; return its path."""
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(dead_letter, "DB_CATALOG_PATH", db)
    return db


def test_table_and_index_created(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``init_dlq_table`` creates the table and its ``idx_dlq_task_id`` index."""
    db = _isolate_catalog(tmp_path, monkeypatch)
    asyncio.run(init_dlq_table())

    conn = sqlite3.connect(db)
    objs = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name IN ('dead_letter_tasks', 'idx_dlq_task_id')"
        )
    }
    conn.close()
    assert objs == {"dead_letter_tasks", "idx_dlq_task_id"}


def test_decorator_intercepts_and_dead_letters(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled node exception → L1→L2 promote + one DLQ row + re-raise."""
    db = _isolate_catalog(tmp_path, monkeypatch)
    promotions: List[str] = []
    monkeypatch.setattr(
        checkpoint_manager, "promote", lambda tid: promotions.append(tid)
    )

    @dead_letter_decorator("coder_agent")
    async def _crashing_node(state: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("boom in coder")

    async def _run() -> None:
        await init_dlq_table()
        with pytest.raises(ValueError, match="boom in coder"):
            await _crashing_node({"task_id": "task-xyz"})

    asyncio.run(_run())

    assert promotions == ["task-xyz"]  # L1→L2 promotion attempted
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT task_id, thread_id, failed_node, exception_class, "
        "exception_message FROM dead_letter_tasks"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0] == (
        "task-xyz", "task-xyz", "coder_agent", "ValueError", "boom in coder",
    )


def test_resume_lifecycle_is_idempotent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seeded episode is pending until resolved, then never resurfaces."""
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> tuple[str, List[Any], List[Any]]:
        await init_dlq_table()
        episode_id = await save_dead_letter(
            task_id="task-r", thread_id="task-r", failed_node="apply_patch",
            exc=RuntimeError("crash"), state={"task_id": "task-r"},
        )
        before = await get_pending_dlqs("task-r")
        await mark_dlq_resolved(episode_id)
        after = await get_pending_dlqs("task-r")
        return episode_id, before, after

    episode_id, before, after = asyncio.run(_run())

    assert len(before) == 1
    assert before[0].episode_id == episode_id
    assert before[0].failed_node == "apply_patch"
    assert before[0].exception_class == "RuntimeError"
    assert after == []  # resolved episode is a resume no-op
