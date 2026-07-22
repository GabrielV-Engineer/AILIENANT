# ailienant-core/core/telemetry.py
#
# Phase 2.23 — Append-only SQLite routing audit trail.

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("TELEMETRY")

_DEFAULT_DB_PATH: Path = Path("data") / "telemetry.sqlite"

_conn: Optional[sqlite3.Connection] = None
_lock: threading.Lock = threading.Lock()

# --- Read-path hardening (Phase 7.9.B.6) -----------------------------------
# S6: deepest OFFSET we will ever serve, so a spammed/huge-offset request can
# never hold the global _lock long enough to starve telemetry writes.
_OFFSET_HARD_CAP: int = 10_000
_LIMIT_MAX: int = 200
# S5: cap the text fed to the masking regex so a giant log line cannot trigger
# catastrophic backtracking (ReDoS) and pin a CPU.
_MASK_INPUT_CAP: int = 2_000
_REDACTED: str = "***REDACTED***"

# S1/S5: ReDoS-safe secret patterns — bounded quantifiers, no nesting. The
# key=value pattern uses a non-greedy value capture so a single mask never
# swallows an entire line that holds several secrets.
_KV_SECRET_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)(\S+?)(?=\s|$)"
)
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),          # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{8,}"),               # AWS access key id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),  # bearer tokens
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),           # long hex blobs
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),   # long base64 blobs
]


def _mask_sensitive(text: Optional[str]) -> Optional[str]:
    """Redact secrets from a free-text field before it leaves the server (S1).

    ReDoS-safe (S5): input is truncated to ``_MASK_INPUT_CAP`` chars before any
    regex runs, and every pattern uses bounded/non-nested quantifiers.
    """
    if not text:
        return text
    snippet = text[:_MASK_INPUT_CAP]
    snippet = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", snippet)
    for pat in _SECRET_PATTERNS:
        snippet = pat.sub(_REDACTED, snippet)
    if len(text) > _MASK_INPUT_CAP:
        snippet += "…"
    return snippet


def _clamp_pagination(limit: int, offset: int) -> Tuple[int, int]:
    """S4+S6: coerce to int, clamp limit to 1.._LIMIT_MAX and offset to 0..cap."""
    safe_limit = max(1, min(int(limit), _LIMIT_MAX))
    safe_offset = max(0, min(int(offset), _OFFSET_HARD_CAP))
    return safe_limit, safe_offset

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
    hardware_constraint TEXT,
    project_id          TEXT
);

-- OOM Cascade telemetry. One row per local→cloud rescue swap emitted by
-- tools/llm_gateway.py::_oom_cascade.
CREATE TABLE IF NOT EXISTS oom_fallback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id        TEXT,
    event             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    original_model    TEXT NOT NULL,
    fallback_model    TEXT NOT NULL,
    tokens_at_failure INTEGER,
    swap_latency_ms   REAL,
    project_id        TEXT
);
"""

# Additive project_id migration + filter indexes. CREATE TABLE IF NOT EXISTS leaves
# a pre-existing telemetry DB untouched, so the column is added via a PRAGMA-guarded
# ALTER (SQLite has no ADD COLUMN IF NOT EXISTS); the index keeps the dashboard's
# per-project ``WHERE project_id = ?`` read off an O(N) scan of the growing ledger.
_PROJECT_MIGRATIONS: Tuple[str, ...] = ("routing_decisions", "oom_fallback_events")


def init_telemetry_db(db_path: Union[str, Path] = _DEFAULT_DB_PATH) -> None:
    """Open connection, apply WAL mode, create schema. Idempotent."""
    global _conn
    resolved = Path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(resolved), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.executescript(_DDL)
    for table in _PROJECT_MIGRATIONS:
        cols = {row[1] for row in _conn.execute(f"PRAGMA table_info({table})")}
        if "project_id" not in cols:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
        _conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_project ON {table}(project_id)"
        )
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
    project_id: Optional[str] = None,
) -> None:
    """Insert one routing audit record. Silently no-ops if DB not initialized.

    ``project_id`` scopes the row for the dashboard's per-project routing view;
    callers pass ``state["project_id"]``. A ``None``/blank id stores NULL and is
    simply excluded when a project filter is applied.
    """
    # Forensic file log FIRST — independent of the SQLite connection, so a
    # "database is locked" error (or an uninitialised DB) never costs us the
    # trace. The sink enqueues in O(1) and writes off-loop; it never raises.
    from core.telemetry_log import log_node_transition
    log_node_transition(session_id=session_id, source=source, target=target, reason=reason)
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO routing_decisions "
                "(session_id, source_node, target_node, reason, css_score, tci_score, "
                "hardware_constraint, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, source, target, reason, css, tci, hw, project_id or None),
            )
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Telemetry write failed: %s", exc)


async def log_oom_event(
    *,
    reason: str,
    original_model: str,
    fallback_model: str,
    tokens_at_failure: int,
    swap_latency_ms: float,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert one ``oom_fallback`` row recording a local→cloud rescue swap.

    Phase 6.8 — called from ``tools/llm_gateway.py::_oom_cascade``. The async
    signature matches the async cascade call site; the body is a sync SQLite
    write under the shared thread-lock, mirroring ``log_routing_decision``.
    Silently no-ops if the telemetry DB has not been initialised.
    """
    session_id = str(state.get("task_id", "")) if state else ""
    project_id = str(state.get("project_id", "")) if state else ""
    # Forensic file log FIRST — see log_routing_decision for the rationale.
    from core.telemetry_log import log_node_transition
    log_node_transition(
        session_id=session_id,
        source=original_model,
        target=fallback_model,
        reason=f"oom_fallback: {reason}",
    )
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO oom_fallback_events "
                "(session_id, event, reason, original_model, fallback_model, "
                "tokens_at_failure, swap_latency_ms, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, "oom_fallback", reason, original_model,
                    fallback_model, tokens_at_failure, swap_latency_ms,
                    project_id or None,
                ),
            )
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("OOM telemetry write failed: %s", exc)


def recent_routing_decisions(
    limit: int = 50, offset: int = 0, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return recent routing decisions, newest first. Read path for the dashboard.

    Pagination is clamped (S4+S6) and the free-text ``reason``/``hardware_constraint``
    fields are secret-masked (S1) before returning. When ``project_id`` is given the
    result is scoped to that project (index-backed); omitted returns all. Returns
    ``[]`` if the DB is not initialised.
    """
    if _conn is None:
        return []
    safe_limit, safe_offset = _clamp_pagination(limit, offset)
    where = "WHERE project_id = ? " if project_id else ""
    params: Tuple[Any, ...] = (
        (project_id, safe_limit, safe_offset) if project_id else (safe_limit, safe_offset)
    )
    with _lock:
        try:
            cursor = _conn.execute(
                "SELECT id, timestamp, session_id, source_node, target_node, "
                "reason, css_score, tci_score, hardware_constraint, project_id "
                f"FROM routing_decisions {where}ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.warning("Telemetry read failed: %s", exc)
            return []
    return [
        {
            "id": r[0],
            "timestamp": r[1],
            "session_id": r[2],
            "source_node": r[3],
            "target_node": r[4],
            "reason": _mask_sensitive(r[5]),
            "css_score": r[6],
            "tci_score": r[7],
            "hardware_constraint": _mask_sensitive(r[8]),
            "project_id": r[9],
        }
        for r in rows
    ]


def recent_oom_events(
    limit: int = 50, offset: int = 0, project_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return recent OOM rescue-swap events, newest first. Read path for the dashboard.

    Same clamping (S4+S6) and masking (S1) discipline as
    :func:`recent_routing_decisions`, and the same optional index-backed
    ``project_id`` scoping. Returns ``[]`` if the DB is not initialised.
    """
    if _conn is None:
        return []
    safe_limit, safe_offset = _clamp_pagination(limit, offset)
    where = "WHERE project_id = ? " if project_id else ""
    params: Tuple[Any, ...] = (
        (project_id, safe_limit, safe_offset) if project_id else (safe_limit, safe_offset)
    )
    with _lock:
        try:
            cursor = _conn.execute(
                "SELECT id, timestamp, session_id, event, reason, original_model, "
                "fallback_model, tokens_at_failure, swap_latency_ms, project_id "
                f"FROM oom_fallback_events {where}ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.warning("OOM telemetry read failed: %s", exc)
            return []
    return [
        {
            "id": r[0],
            "timestamp": r[1],
            "session_id": r[2],
            "event": r[3],
            "reason": _mask_sensitive(r[4]),
            "original_model": r[5],
            "fallback_model": r[6],
            "tokens_at_failure": r[7],
            "swap_latency_ms": r[8],
            "project_id": r[9],
        }
        for r in rows
    ]


def shutdown_telemetry_db() -> None:
    """Close the connection. Call from lifespan shutdown or test teardown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
