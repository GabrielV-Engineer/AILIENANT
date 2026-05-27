"""
brain/checkpoint.py — Tiered Checkpointing (L1 MemorySaver + L2 SQLite Promotion).

L1: MemorySaver (inherited) — all graph node writes go here. Zero IOPS during execution.
L2: Custom SQLite snapshot — one UPSERT transaction per completed graph run (promotion).

Lifecycle (wired to FastAPI lifespan):
    startup  → checkpoint_manager.initialize()   # binds L2 connection + applies WAL pragmas
    shutdown → checkpoint_manager.close()        # after WALCheckpointer.force_truncate()
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger("HYBRID_CHECKPOINTER")


# Phase 7.11.8 (ADR-706 §4.5g) — typed summary returned by list_checkpoints().
# Kept as a small dataclass instead of a pydantic model so this module has no
# new dependency surface; the REST layer (`api/sessions.py`) re-projects this
# into a pydantic response model before serializing to JSON.
@dataclass(frozen=True)
class CheckpointSummary:
    """One row of the per-thread checkpoint chain.

    ``termination_reason`` is read from the LangGraph checkpoint metadata blob
    (currently only ``"user_abort"`` is populated by the Phase 7.11.3 abort
    mesh, but the field is intentionally typed as ``Optional[str]`` so future
    fault classes — ``"budget_kill"``, ``"timeout"``, ``"drift_detected"`` —
    can land without a schema bump).
    """

    checkpoint_id: str
    parent_id: Optional[str]
    promoted_at: float
    termination_reason: Optional[str]

DB_PATH = "ailienant_state.sqlite"

_L2_DDL = """
CREATE TABLE IF NOT EXISTS hybrid_checkpoints (
    thread_id       TEXT NOT NULL,
    checkpoint_ns   TEXT NOT NULL DEFAULT '',
    checkpoint_id   TEXT NOT NULL,
    parent_id       TEXT,
    ckpt_type       TEXT NOT NULL,
    ckpt_blob       BLOB NOT NULL,
    meta_type       TEXT NOT NULL,
    meta_blob       BLOB NOT NULL,
    promoted_at     REAL NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS hybrid_writes_l2 (
    thread_id       TEXT NOT NULL,
    checkpoint_ns   TEXT NOT NULL DEFAULT '',
    checkpoint_id   TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    write_idx       INTEGER NOT NULL,
    channel         TEXT NOT NULL,
    val_type        TEXT NOT NULL,
    val_blob        BLOB NOT NULL,
    path            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx)
);
"""


class HybridCheckpointer(MemorySaver):
    """L1=MemorySaver (inherited, zero IOPS), L2=SQLite promotion-only (one write per run).

    All graph node writes go to L1 via inherited MemorySaver methods.
    After each graph run completes, task_service calls promote(thread_id) to
    persist the latest checkpoint to L2 in a single SQLite transaction.
    On server restart, recover(thread_id) seeds L1 from L2 for session resumption.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        super().__init__()
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._is_writing: bool = False

    def initialize(self) -> None:
        """Open L2 connection, apply WAL pragmas, create schema. Call once from lifespan startup."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        for pragma in (
            "PRAGMA journal_mode=WAL;",
            "PRAGMA synchronous=NORMAL;",
            "PRAGMA mmap_size=268435456;",
            "PRAGMA cache_size=-64000;",
        ):
            self._conn.execute(pragma)
        self._conn.executescript(_L2_DDL)
        self._conn.commit()

    def flush_all_sessions(self) -> None:
        """Promote every active L1 session to L2 before shutdown.

        MemorySaver.storage is a defaultdict keyed by thread_id. Iterating its
        keys gives us all sessions that have in-memory checkpoint data. We promote
        each one so no graph state is lost on unclean shutdown (e.g. Ctrl+C during
        an active graph run that hadn't reached task_service.promote() yet).

        Call this BEFORE force_truncate() so the freshly-promoted rows are included
        in the WAL checkpoint.
        """
        thread_ids = list(self.storage.keys())
        promoted = 0
        for thread_id in thread_ids:
            try:
                self.promote(thread_id)
                promoted += 1
            except Exception as exc:
                logger.warning(
                    "flush_all_sessions: promote failed for thread_id=%s: %s", thread_id, exc
                )
        logger.info("L1→L2 flush complete (%d/%d sessions promoted).", promoted, len(thread_ids))

    def close(self) -> None:
        """Close L2 connection. Call from lifespan shutdown after WALCheckpointer.force_truncate()."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def promote(self, thread_id: str) -> None:
        """Serialize the latest L1 checkpoint for thread_id to L2 in one SQLite transaction.

        Uses the public get_tuple() API + inherited serde for serialization so the
        promotion code is decoupled from MemorySaver's private storage layout.
        """
        if self._conn is None:
            return
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        ct = self.get_tuple(config)
        if ct is None:
            return

        ckpt_type, ckpt_blob = self.serde.dumps_typed(ct.checkpoint)
        meta_type, meta_blob = self.serde.dumps_typed(ct.metadata)
        cid: str = ct.config["configurable"]["checkpoint_id"]
        ns: str = ct.config["configurable"].get("checkpoint_ns", "")
        parent_id: Optional[str] = (
            ct.parent_config["configurable"].get("checkpoint_id")
            if ct.parent_config else None
        )

        self._is_writing = True
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO hybrid_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                    (thread_id, ns, cid, parent_id,
                     ckpt_type, ckpt_blob, meta_type, meta_blob,
                     time.monotonic()),
                )
                if ct.pending_writes:
                    for pw in ct.pending_writes:
                        task_id, channel, value = pw[0], pw[1], pw[2]
                        vt, vb = self.serde.dumps_typed(value)
                        self._conn.execute(
                            "INSERT OR REPLACE INTO hybrid_writes_l2 VALUES (?,?,?,?,?,?,?,?,?)",
                            (thread_id, ns, cid, task_id, 0, channel, vt, vb, ""),
                        )
        finally:
            self._is_writing = False

    def recover(self, thread_id: str) -> None:
        """Restore the latest L2 checkpoint for thread_id into L1 (MemorySaver).

        Called when a client reconnects after a server restart. Seeds L1 so the
        graph can resume from the last promoted checkpoint without hitting L2 again.
        """
        if self._conn is None:
            return
        row = self._conn.execute(
            "SELECT checkpoint_ns, checkpoint_id, parent_id, "
            "ckpt_type, ckpt_blob, meta_type, meta_blob "
            "FROM hybrid_checkpoints WHERE thread_id=? "
            "ORDER BY promoted_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row is None:
            return
        ns, cid, parent_id, ckpt_type, ckpt_blob, meta_type, meta_blob = row
        checkpoint = self.serde.loads_typed((ckpt_type, ckpt_blob))
        metadata = self.serde.loads_typed((meta_type, meta_blob))
        restore_config: RunnableConfig = {"configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": cid,
        }}
        self.put(restore_config, checkpoint, metadata,
                 checkpoint.get("channel_versions", {}))
        if parent_id:
            parent_config: RunnableConfig = {"configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": parent_id,
            }}
            self.put(parent_config, checkpoint, metadata, {})

    # ------------------------------------------------------------------
    # Phase 7.11.8 (ADR-706 §4.5g) — Time-travel API
    # ------------------------------------------------------------------
    # These three methods unlock the "branch a conversation from an earlier
    # checkpoint" UX. They are additive — `promote` / `recover` continue to
    # serve the latest-only resume path used by the dead-letter handler and
    # the lifespan-shutdown flush. The L2 schema already carries `parent_id`
    # since Phase 6.x, so this is purely a read-side / fork-write addition.

    def list_checkpoints(self, thread_id: str) -> List[CheckpointSummary]:
        """Return the chronological checkpoint chain for ``thread_id``.

        Used by ``GET /api/v1/sessions/{id}/checkpoints`` to populate the
        time-travel picker UI. Each row's ``termination_reason`` is extracted
        from the deserialized metadata blob so the frontend can render an
        ``⏹ aborted`` badge alongside checkpoints captured by the Phase
        7.11.3 emergency-savepoint path. Returns ``[]`` if the L2 connection
        is not initialised or no rows match — never raises on empty.
        """
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT checkpoint_id, parent_id, promoted_at, meta_type, meta_blob "
            "FROM hybrid_checkpoints WHERE thread_id=? "
            "ORDER BY promoted_at ASC",
            (thread_id,),
        ).fetchall()
        out: List[CheckpointSummary] = []
        for cid, parent_id, promoted_at, meta_type, meta_blob in rows:
            termination: Optional[str] = None
            try:
                meta = self.serde.loads_typed((meta_type, meta_blob))
                # Metadata is a dict-like LangGraph payload; the abort mesh
                # tags it via `metadata={"termination_reason": "user_abort"}`.
                if isinstance(meta, dict):
                    raw = meta.get("termination_reason")
                    if isinstance(raw, str):
                        termination = raw
            except Exception:  # noqa: BLE001 — a corrupt row never breaks the listing.
                logger.warning(
                    "list_checkpoints: failed to deserialize metadata for %s/%s",
                    thread_id, cid,
                )
            out.append(CheckpointSummary(
                checkpoint_id=cid,
                parent_id=parent_id,
                promoted_at=promoted_at,
                termination_reason=termination,
            ))
        return out

    def get_checkpoint(
        self, thread_id: str, checkpoint_id: str,
    ) -> Optional[CheckpointTuple]:
        """Look up a specific ``(thread_id, checkpoint_id)`` tuple in L2.

        Returns the inflated ``CheckpointTuple`` (the same shape
        ``MemorySaver.get_tuple`` produces) or ``None`` if the row is
        missing. Used by ``branch_from`` to read the source row before
        writing the fork.
        """
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT checkpoint_ns, parent_id, "
            "ckpt_type, ckpt_blob, meta_type, meta_blob "
            "FROM hybrid_checkpoints WHERE thread_id=? AND checkpoint_id=?",
            (thread_id, checkpoint_id),
        ).fetchone()
        if row is None:
            return None
        ns, parent_id, ckpt_type, ckpt_blob, meta_type, meta_blob = row
        checkpoint = self.serde.loads_typed((ckpt_type, ckpt_blob))
        metadata = self.serde.loads_typed((meta_type, meta_blob))
        config: RunnableConfig = {"configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }}
        parent_config: Optional[RunnableConfig] = None
        if parent_id:
            parent_config = {"configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": parent_id,
            }}
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=None,
        )

    def branch_from(
        self,
        from_thread_id: str,
        from_checkpoint_id: str,
        new_thread_id: str,
    ) -> bool:
        """Fork: copy ``(from_thread_id, from_checkpoint_id)`` into a new thread.

        Stores a row under ``new_thread_id`` re-using the source's
        ``checkpoint_id`` and blobs verbatim. ``parent_id`` on the new row
        points to the SOURCE checkpoint (NOT the source's own parent), so a
        future lineage walk can cross the branch boundary. Also seeds L1
        (MemorySaver) so the next ``ainvoke(config={"thread_id":
        new_thread_id, ...})`` finds state in-process without an extra L2
        round-trip.

        Returns ``True`` on success; ``False`` if the source row was missing
        (caller should propagate that as a 404-equivalent to the UI).
        """
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT checkpoint_ns, ckpt_type, ckpt_blob, meta_type, meta_blob "
            "FROM hybrid_checkpoints WHERE thread_id=? AND checkpoint_id=?",
            (from_thread_id, from_checkpoint_id),
        ).fetchone()
        if row is None:
            return False
        ns, ckpt_type, ckpt_blob, meta_type, meta_blob = row

        self._is_writing = True
        try:
            with self._conn:
                # parent_id points to the source checkpoint so the chain walk
                # can cross the branch boundary.
                self._conn.execute(
                    "INSERT OR REPLACE INTO hybrid_checkpoints VALUES "
                    "(?,?,?,?,?,?,?,?,?)",
                    (new_thread_id, ns, from_checkpoint_id, from_checkpoint_id,
                     ckpt_type, ckpt_blob, meta_type, meta_blob,
                     time.monotonic()),
                )
        finally:
            self._is_writing = False

        # Seed L1 so the next ainvoke on the new thread can resume in-process.
        try:
            checkpoint = self.serde.loads_typed((ckpt_type, ckpt_blob))
            metadata = self.serde.loads_typed((meta_type, meta_blob))
            seed_config: RunnableConfig = {"configurable": {
                "thread_id": new_thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": from_checkpoint_id,
            }}
            self.put(seed_config, checkpoint, metadata,
                     checkpoint.get("channel_versions", {}))
        except Exception as exc:  # noqa: BLE001 — L1 seed is best-effort; L2 row is canonical.
            logger.warning(
                "branch_from: L1 seed failed for %s/%s (L2 row written OK): %s",
                new_thread_id, from_checkpoint_id, exc,
            )
        return True

    @property
    def conn(self) -> Optional[sqlite3.Connection]:
        """Exposed for WALCheckpointer (db_maintenance.py) to run PRAGMA wal_checkpoint."""
        return self._conn

    @property
    def is_writing(self) -> bool:
        """Exposed for WALCheckpointer to defer checkpoint during active L2 promotion."""
        return self._is_writing


# Global singleton — imported by brain/engine.py and core/db_maintenance.py
hybrid_checkpointer = HybridCheckpointer()

# Alias: engine.py and main.py import `checkpoint_manager` — no change needed there
checkpoint_manager = hybrid_checkpointer
