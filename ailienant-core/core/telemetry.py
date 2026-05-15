# ailienant-core/core/telemetry.py
#
# Phase 2.23 — Append-only SQLite routing audit trail.

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("TELEMETRY")

_DEFAULT_DB_PATH: Path = Path("data") / "telemetry.sqlite"

_conn: Optional[sqlite3.Connection] = None
_lock: threading.Lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id          TEXT,
    source_node         TEXT,
    target_node         TEXT,
    reason              TEXT,
    css_score           REAL,
    tci_score           REAL,
    hardware_constraint TEXT
);
"""


def init_telemetry_db(db_path: Union[str, Path] = _DEFAULT_DB_PATH) -> None:
    """Open connection, apply WAL mode, create schema. Idempotent."""
    global _conn
    resolved = Path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(resolved), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.executescript(_DDL)
    _conn.commit()
    logger.info("Telemetry DB initialized at %s.", resolved)


def log_routing_decision(
    session_id: str,
    source: str,
    target: str,
    reason: str,
    css: Optional[float] = None,
    tci: Optional[float] = None,
    hw: Optional[str] = None,
) -> None:
    """Insert one routing audit record. Silently no-ops if DB not initialized."""
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO routing_decisions "
                "(session_id, source_node, target_node, reason, css_score, tci_score, hardware_constraint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, source, target, reason, css, tci, hw),
            )
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Telemetry write failed: %s", exc)


def shutdown_telemetry_db() -> None:
    """Close the connection. Call from lifespan shutdown or test teardown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
