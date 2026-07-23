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

-- Per-request end-to-end latency. One row per _run_coding_task invocation,
-- written from its finally block. Percentiles are computed over a bounded
-- most-recent window at read time, so the read never scans the whole ledger.
-- TODO(retention): DEBT-120 — append-only; wire GC into core/janitor.py.
CREATE TABLE IF NOT EXISTS request_latency (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id  TEXT,
    project_id  TEXT,
    duration_ms REAL,
    outcome     TEXT
);

-- Docker container lifecycle events (started/stopped/removed). Machine-global
-- (no project dimension), emitted from core/sandbox.py's async wrappers.
-- TODO(retention): DEBT-120 — append-only; wire GC into core/janitor.py.
CREATE TABLE IF NOT EXISTS container_lifecycle (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
    event        TEXT NOT NULL,
    container_id TEXT,
    image        TEXT,
    tier         TEXT
);
"""

# Additive project_id migration + filter indexes. CREATE TABLE IF NOT EXISTS leaves
# a pre-existing telemetry DB untouched, so the column is added via a PRAGMA-guarded
# ALTER (SQLite has no ADD COLUMN IF NOT EXISTS); the index keeps the dashboard's
# per-project ``WHERE project_id = ?`` read off an O(N) scan of the growing ledger.
_PROJECT_MIGRATIONS: Tuple[str, ...] = ("routing_decisions", "oom_fallback_events", "request_latency")

# Latency read window: percentiles are computed over at most this many most-recent
# rows (fetched with an SQL-level LIMIT), keeping the O(N log N) sort trivial and
# off the ledger's growth curve. ``recent`` is capped smaller still for the sparkline.
_LATENCY_WINDOW: int = 500
_LATENCY_RECENT_CAP: int = 60


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


# --- Request latency (P50/P95) -------------------------------------------------


def _percentile(sorted_vals: List[float], q: float) -> float:
    """Linear-interpolation percentile over an ascending-sorted list.

    Hand-rolled to avoid a numpy/scipy dependency for this narrow need (charter
    §9). ``q`` is 0..100. Returns 0.0 for an empty list.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    rank = (q / 100.0) * (n - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 < n:
        return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])
    return sorted_vals[lo]


def log_request_latency(
    session_id: str,
    project_id: Optional[str],
    duration_ms: float,
    outcome: str,
) -> None:
    """Insert one end-to-end request-latency row. No-ops if DB not initialized.

    ``project_id`` scopes the row for the dashboard's per-project latency view.
    Best-effort: a write failure is logged, never raised, so this is safe to call
    from a caller's ``finally`` block.
    """
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO request_latency "
                "(session_id, project_id, duration_ms, outcome) VALUES (?, ?, ?, ?)",
                (session_id, project_id or None, float(duration_ms), outcome),
            )
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Latency telemetry write failed: %s", exc)


def latency_percentiles(
    project_id: Optional[str] = None, window: int = _LATENCY_WINDOW
) -> Dict[str, Any]:
    """Summarize request latency over a bounded most-recent window.

    The window is applied as an SQL-level ``ORDER BY id DESC LIMIT ?`` so the
    percentile sort never touches more than ``window`` rows regardless of ledger
    size. ``recent`` holds the newest ≤``_LATENCY_RECENT_CAP`` durations in
    chronological order for a sparkline. Empty/uninitialised → zeros, never raises.
    """
    empty: Dict[str, Any] = {
        "count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0,
        "max_ms": 0.0, "avg_ms": 0.0, "recent": [],
    }
    if _conn is None:
        return empty
    safe_window = max(1, min(int(window), _LATENCY_WINDOW))
    where = "WHERE project_id = ? " if project_id else ""
    params: Tuple[Any, ...] = (project_id, safe_window) if project_id else (safe_window,)
    with _lock:
        try:
            cursor = _conn.execute(
                f"SELECT duration_ms FROM request_latency {where}ORDER BY id DESC LIMIT ?",
                params,
            )
            rows = [float(r[0]) for r in cursor.fetchall() if r[0] is not None]
        except sqlite3.Error as exc:
            logger.warning("Latency telemetry read failed: %s", exc)
            return empty
    if not rows:
        return empty
    # rows are newest-first; the sparkline wants oldest→newest.
    recent = [round(v, 2) for v in reversed(rows[:_LATENCY_RECENT_CAP])]
    ordered = sorted(rows)
    return {
        "count": len(ordered),
        "p50_ms": round(_percentile(ordered, 50), 2),
        "p95_ms": round(_percentile(ordered, 95), 2),
        "p99_ms": round(_percentile(ordered, 99), 2),
        "max_ms": round(ordered[-1], 2),
        "avg_ms": round(sum(ordered) / len(ordered), 2),
        "recent": recent,
    }


# --- Docker container lifecycle (machine-global) -------------------------------


def log_container_event(event: str, container_id: str, image: str, tier: str) -> None:
    """Append one Docker container lifecycle event (started/stopped/removed).

    Machine-global (no project dimension). Best-effort: never raises, so an emit
    from the sandbox lifecycle path can never affect the cage.

    TODO(retention): DEBT-120 — append-only; wire GC into core/janitor.py.
    """
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO container_lifecycle "
                "(event, container_id, image, tier) VALUES (?, ?, ?, ?)",
                (event, container_id, image, tier),
            )
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Container lifecycle telemetry write failed: %s", exc)


def recent_container_events(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent container lifecycle events, newest first. Machine-global.

    Clamped read; returns ``[]`` if the DB is not initialised.
    """
    if _conn is None:
        return []
    safe_limit, _ = _clamp_pagination(limit, 0)
    with _lock:
        try:
            cursor = _conn.execute(
                "SELECT id, timestamp, event, container_id, image, tier "
                "FROM container_lifecycle ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            )
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.warning("Container lifecycle read failed: %s", exc)
            return []
    return [
        {"id": r[0], "timestamp": r[1], "event": r[2],
         "container_id": r[3], "image": r[4], "tier": r[5]}
        for r in rows
    ]


def shutdown_telemetry_db() -> None:
    """Close the connection. Call from lifespan shutdown or test teardown."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
