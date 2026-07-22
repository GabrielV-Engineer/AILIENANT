"""Phase 6.4 — Dead Letter Queue & crash-resilient state capture.

When a LangGraph node raises an unhandled exception, :func:`dead_letter_decorator`
catches it, promotes the L1 (in-memory) checkpoint to L2 (disk) so the trajectory
survives, persists a ``dead_letter_tasks`` row, and **re-raises** so LangGraph's
native error handling still registers the failure. ``POST /api/v1/task/resume/
{task_id}`` (``main.py``) later re-hydrates the L2 checkpoint and re-invokes the
graph.

The ``state_snapshot_blob_hash`` is an integrity reference only — ``blob_storage``
is RAM-backed, so the authoritative resume state is the L2 SQLite checkpoint, not
the blob.

Blueprint reference: §5 (ACID Transactions, DLQ, Resume API).
"""
from __future__ import annotations

import functools
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiosqlite
from pydantic import BaseModel

from core.blob_storage import blob_storage
from shared.config import DB_CATALOG_PATH

logger = logging.getLogger("DEAD_LETTER")

# ── Schema ──────────────────────────────────────────────────────────────────
# resolved_at is NULL while an episode is recoverable; stamped on a successful
# resume so a repeated resume call is a deterministic no-op (idempotency).

_DLQ_DDL = """CREATE TABLE IF NOT EXISTS dead_letter_tasks (
    episode_id               TEXT    PRIMARY KEY,
    task_id                  TEXT    NOT NULL,
    thread_id                TEXT    NOT NULL,
    failed_node              TEXT    NOT NULL,
    exception_class          TEXT    NOT NULL,
    exception_message        TEXT    NOT NULL,
    state_snapshot_blob_hash TEXT    NOT NULL,
    created_at               INTEGER NOT NULL,
    resolved_at              INTEGER,
    project_id               TEXT
)"""

_DLQ_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_dlq_task_id "
    "ON dead_letter_tasks(task_id, created_at)"
)

# Project filter index for the dashboard's per-project recovery view.
_DLQ_PROJECT_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_dlq_project "
    "ON dead_letter_tasks(project_id)"
)

_EXC_MSG_CAP: int = 2000  # truncation cap for exception_message


class DeadLetterRecord(BaseModel):
    """One persisted DLQ episode — a node crash awaiting resume."""

    episode_id: str
    task_id: str
    thread_id: str
    failed_node: str
    exception_class: str
    exception_message: str
    state_snapshot_blob_hash: str
    created_at: int
    resolved_at: Optional[int] = None
    project_id: Optional[str] = None


async def init_dlq_table() -> None:
    """Idempotent ``CREATE TABLE``/``CREATE INDEX`` for the DLQ. Called once from
    the FastAPI lifespan startup, after ``catalog_db.init_db()``."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(_DLQ_DDL)
        # Additive migration: add the nullable project_id column to a pre-existing
        # table (CREATE TABLE IF NOT EXISTS won't; SQLite has no ADD COLUMN IF NOT
        # EXISTS). Old rows read back NULL and are excluded under a project filter.
        async with db.execute("PRAGMA table_info(dead_letter_tasks)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "project_id" not in cols:
            await db.execute("ALTER TABLE dead_letter_tasks ADD COLUMN project_id TEXT")
        await db.execute(_DLQ_IDX)
        await db.execute(_DLQ_PROJECT_IDX)
        await db.commit()


async def save_dead_letter(
    *,
    task_id: str,
    thread_id: str,
    failed_node: str,
    exc: BaseException,
    state: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist a DLQ row and return the new ``episode_id``.

    The state snapshot is JSON-coerced (``default=str`` absorbs Pydantic models
    and other non-serialisable objects) and interned in the blake2b CAS. The
    resulting hash is an integrity marker — authoritative resume state lives in
    the L2 checkpoint.
    """
    episode_id = uuid.uuid4().hex
    blob_hash = ""
    # Tag the crashed episode with its project so the dashboard's recovery panel
    # can scope to the active project. Read from the same state the snapshot uses.
    project_id = str(state.get("project_id", "")) if state else ""
    if state is not None:
        try:
            blob_hash = blob_storage.put(
                json.dumps(state, default=str, sort_keys=True)
            )
        except Exception as snap_exc:  # noqa: BLE001 — snapshot is best-effort
            logger.warning(
                "DLQ state snapshot failed for task=%s: %s", task_id, snap_exc
            )
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT INTO dead_letter_tasks (episode_id, task_id, thread_id, "
            "failed_node, exception_class, exception_message, "
            "state_snapshot_blob_hash, created_at, resolved_at, project_id) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,?)",
            (
                episode_id, task_id, thread_id, failed_node,
                type(exc).__name__, str(exc)[:_EXC_MSG_CAP],
                blob_hash, int(time.time()), project_id or None,
            ),
        )
        await db.commit()
    logger.error(
        "DLQ episode %s recorded: task=%s node=%s exc=%s",
        episode_id, task_id, failed_node, type(exc).__name__,
    )
    return episode_id


async def get_pending_dlqs(
    task_id: Optional[str] = None, project_id: Optional[str] = None
) -> List[DeadLetterRecord]:
    """Return unresolved DLQ episodes (newest first), optionally scoped to a
    ``task_id`` and/or ``project_id``. ``resolved_at IS NULL`` is the
    recoverable-episode filter."""
    query = (
        "SELECT episode_id, task_id, thread_id, failed_node, exception_class, "
        "exception_message, state_snapshot_blob_hash, created_at, resolved_at, "
        "project_id "
        "FROM dead_letter_tasks WHERE resolved_at IS NULL"
    )
    params: tuple[str, ...] = ()
    if task_id is not None:
        query += " AND task_id = ?"
        params += (task_id,)
    if project_id is not None:
        query += " AND project_id = ?"
        params += (project_id,)
    query += " ORDER BY created_at DESC"

    records: List[DeadLetterRecord] = []
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    for r in rows:
        raw_resolved = r[8]
        records.append(
            DeadLetterRecord(
                episode_id=str(r[0]),
                task_id=str(r[1]),
                thread_id=str(r[2]),
                failed_node=str(r[3]),
                exception_class=str(r[4]),
                exception_message=str(r[5]),
                state_snapshot_blob_hash=str(r[6]),
                created_at=int(r[7]),
                resolved_at=None if raw_resolved is None else int(raw_resolved),
                project_id=None if r[9] is None else str(r[9]),
            )
        )
    return records


async def mark_dlq_resolved(episode_id: str) -> None:
    """Stamp ``resolved_at`` on an episode so a repeated resume is a no-op."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "UPDATE dead_letter_tasks SET resolved_at = ? WHERE episode_id = ?",
            (int(time.time()), episode_id),
        )
        await db.commit()


# ── The decorator ───────────────────────────────────────────────────────────

_AsyncNode = Callable[..., Awaitable[Dict[str, Any]]]


def dead_letter_decorator(node_name: str) -> Callable[[_AsyncNode], _AsyncNode]:
    """Wrap an async LangGraph node so an unhandled exception is dead-lettered.

    On failure: promote L1→L2 (idempotent UPSERT — Phase 2.7/2.15), persist a
    ``dead_letter_tasks`` row, then **re-raise** so LangGraph still observes the
    crash. Both recovery steps are best-effort and never mask the original
    exception.
    """

    def decorator(fn: _AsyncNode) -> _AsyncNode:
        @functools.wraps(fn)
        async def wrapper(
            state: Dict[str, Any], *args: Any, **kwargs: Any
        ) -> Dict[str, Any]:
            try:
                return await fn(state, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — capture-all → DLQ, re-raise
                task_id = (
                    str(state.get("task_id", "")) if isinstance(state, dict) else ""
                )
                try:
                    from brain.checkpoint import checkpoint_manager
                    checkpoint_manager.promote(task_id)
                except Exception as promo_exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "DLQ L1→L2 promotion failed for task=%s: %s",
                        task_id, promo_exc,
                    )
                try:
                    await save_dead_letter(
                        task_id=task_id,
                        thread_id=task_id,
                        failed_node=node_name,
                        exc=exc,
                        state=state if isinstance(state, dict) else None,
                    )
                except Exception as dlq_exc:  # noqa: BLE001 — never mask original
                    logger.error(
                        "DLQ row write failed for task=%s: %s", task_id, dlq_exc
                    )
                raise

        return wrapper

    return decorator
