# brain/episodic/checkpointing.py
"""
Phase 3.4.3a — MCTS episodic checkpoint audit table.

This module owns the new mcts_episodes SQLite table. It reuses the existing
HybridCheckpointer.promote() path for durable LangGraph state and adds an
MCTS-specific audit row per stable node / prune event.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from brain.checkpoint import checkpoint_manager
from brain.mcts.tree import MCTSNode

logger = logging.getLogger("MCTS_CHECKPOINTER")

_DEFAULT_DB_PATH: str = "ailienant_mcts.sqlite"

_DDL: str = """
CREATE TABLE IF NOT EXISTS mcts_episodes (
    node_id          TEXT PRIMARY KEY,
    parent_id        TEXT,
    thread_id        TEXT,
    mission_outcome  TEXT,
    reward_R         REAL,
    action           TEXT,
    accepted_at      REAL NOT NULL,
    prune_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcts_episodes_thread
    ON mcts_episodes(thread_id);
"""


class MCTSCheckpointer:
    """Persists MCTS stable-node and prune events to an audit SQLite table."""

    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None
        self._db_path: str = _DEFAULT_DB_PATH

    def initialize(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        """Open the SQLite connection, apply WAL pragmas, create the schema."""
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        for pragma in (
            "PRAGMA journal_mode=WAL;",
            "PRAGMA synchronous=NORMAL;",
            "PRAGMA mmap_size=268435456;",
            "PRAGMA cache_size=-64000;",
        ):
            self._conn.execute(pragma)
        self._conn.executescript(_DDL)
        self._conn.commit()
        logger.info("MCTSCheckpointer initialized at %s.", db_path)

    def close(self) -> None:
        """Close the SQLite connection. Safe to call multiple times."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def record_stable(self, node: MCTSNode, thread_id: str) -> None:
        """Insert a stable MCTSNode row + promote the LangGraph thread state.

        Reuse + audit: this is the only place the daemon talks to both the
        MCTS audit table AND the LangGraph checkpoint manager.
        """
        if self._conn is None:
            logger.warning("record_stable called before initialize(); no-op.")
            return
        outcome: str = node.mission_state.outcome
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO mcts_episodes "
                "(node_id, parent_id, thread_id, mission_outcome, "
                " reward_R, action, accepted_at, prune_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    node.node_id,
                    node.parent_id,
                    thread_id,
                    outcome,
                    node.reward,
                    node.action,
                    time.time(),
                ),
            )
        checkpoint_manager.promote(thread_id)
        logger.info(
            "MCTS stable recorded: node=%s.. thread=%s reward=%.3f",
            node.node_id[:8], thread_id, node.reward,
        )

    def record_prune(self, node_id: str, reason: str) -> None:
        """Record a prune event; UPDATE existing row or INSERT a prune-only stub."""
        if self._conn is None:
            logger.warning("record_prune called before initialize(); no-op.")
            return
        with self._conn:
            cur = self._conn.execute(
                "UPDATE mcts_episodes SET prune_reason=? WHERE node_id=?",
                (reason, node_id),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT OR REPLACE INTO mcts_episodes "
                    "(node_id, parent_id, thread_id, mission_outcome, "
                    " reward_R, action, accepted_at, prune_reason) "
                    "VALUES (?, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                    (node_id, time.time(), reason),
                )
        logger.info(
            "MCTS prune recorded: node=%s.. reason=%s",
            node_id[:8], reason,
        )


mcts_checkpointer: MCTSCheckpointer = MCTSCheckpointer()
