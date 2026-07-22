"""Phase 6.6 — Append-Only HITL Audit Log (SOC2 cryptographic ledger).

Every Human-in-the-Loop resolution appends one **immutable** row to
``hitl_audit_log``. Rows are blake2b-chained:

    chain_hash = blake2b(prev_chain_hash ‖ audit_id ‖ session_id ‖ request_kind
                         ‖ action_description ‖ proposed_content_hash
                         ‖ resolution ‖ resolved_at)

so any out-of-band mutation of a historical row breaks every subsequent link.
:func:`verify_chain` re-walks a session and raises :class:`AuditChainBrokenError`
on the first inconsistency. ``proposed_content`` is **secrets-scrubbed** before
it is stored or hashed (``shared.logging_filters.SecretsScrubber``, the central
DLP engine — Phase 6.7) — no raw key ever enters the ledger.

Single-write model: one row is appended *at resolution time* (approved /
rejected / timeout all logged), entirely from inside
``api/websocket_manager.py::request_human_approval``.

Realized as module-level functions (not an ``AuditLogger`` class) so the
``from core.audit import get_chain_head`` import in ``core/supervisor.py``
(Phase 6.5) stays valid, matching the ``core/dead_letter.py`` pattern.

Blueprint reference: §7 (Append-Only HITL Audit Chain), §8.2 (scrubber patterns).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import aiosqlite

from shared.config import DB_CATALOG_PATH
from shared.logging_filters import SecretsScrubber

logger = logging.getLogger("AUDIT")


class AuditChainBrokenError(Exception):
    """Raised when ``state["hitl_audit_chain_head"]`` diverges from the DB head.

    A divergence is evidence of an out-of-band mutation of ``hitl_audit_log``
    (the chain is append-only and blake2b-linked). The Supervisor raises this;
    the ``dead_letter_decorator`` catches it and records a recoverable DLQ
    episode so a tampered run is not silently lost.
    """

    def __init__(
        self,
        *,
        state_head: Optional[str],
        db_head: Optional[str],
        task_id: str,
    ) -> None:
        self.state_head: Optional[str] = state_head
        self.db_head: Optional[str] = db_head
        self.task_id: str = task_id
        super().__init__(
            f"HITL audit chain broken for task={task_id}: "
            f"state_head={state_head!r} != db_head={db_head!r}"
        )

    @property
    def diagnostics(self) -> Dict[str, Any]:
        """Structured payload for DLQ snapshots and the AnalystAgent report."""
        return {
            "task_id": self.task_id,
            "state_head": self.state_head,
            "db_head": self.db_head,
        }


# ── Schema ──────────────────────────────────────────────────────────────────

_AUDIT_DDL = """CREATE TABLE IF NOT EXISTS hitl_audit_log (
    audit_id                  TEXT    PRIMARY KEY,
    session_id                TEXT    NOT NULL,
    request_kind              TEXT    NOT NULL,
    action_description        TEXT    NOT NULL,
    proposed_content_scrubbed TEXT,
    proposed_content_hash     TEXT    NOT NULL,
    resolution                TEXT    NOT NULL,
    resolution_comment        TEXT,
    operator_user_email       TEXT,
    prev_chain_hash           TEXT,
    chain_hash                TEXT    NOT NULL,
    resolved_at               INTEGER NOT NULL,
    project_id                TEXT
)"""

_AUDIT_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_session "
    "ON hitl_audit_log(session_id)"
)

# Project filter index for the dashboard's per-project audit view. Without it a
# ``WHERE project_id = ?`` read is an O(N) scan of the whole append-only ledger.
_AUDIT_PROJECT_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_project "
    "ON hitl_audit_log(project_id)"
)

# Field separator for the chain payload — a char that will not occur in any
# action_description or hex digest, so the concatenation is unambiguous.
_SEP: str = "‖"

# Serialises the read-head → compute-hash → INSERT critical section so two
# concurrent HITL resolutions on the same session cannot fork the chain.
_CHAIN_LOCK: asyncio.Lock = asyncio.Lock()

# Known HITL sentinels (Blueprint §3.1 / §7.1). Matched as substrings of the
# action_description, longest-first is not needed — they are disjoint.
_KIND_SENTINELS: Tuple[str, ...] = (
    "BUDGET_OVERFLOW",
    "TOKEN_SPIKE",
    "SANDBOX_DEGRADED_EXEC",
    "DANGEROUS_COMMAND_INTERCEPT",
    "COMMAND_EXECUTE",
    "DRIFT_DETECTED",
    "RESOURCE_CONTENTION",
)

# ── Helpers ─────────────────────────────────────────────────────────────────
# Secrets redaction lives in shared/logging_filters.py (Phase 6.7) — the same
# SecretsScrubber engine scrubs both the logs and this ledger.


def _classify(action_description: str) -> str:
    """Map an ``action_description`` to a ``request_kind`` enum string."""
    upper = action_description.upper()
    for kind in _KIND_SENTINELS:
        if kind in upper:
            return kind
    return "OTHER"


def _compute_chain_hash(
    *,
    prev: Optional[str],
    audit_id: str,
    session_id: str,
    request_kind: str,
    action_description: str,
    proposed_content_hash: str,
    resolution: str,
    resolved_at: int,
) -> str:
    """blake2b link hash over the row payload — see module docstring formula."""
    payload = _SEP.join(
        [
            prev or "",
            audit_id,
            session_id,
            request_kind,
            action_description,
            proposed_content_hash,
            resolution,
            str(resolved_at),
        ]
    )
    return hashlib.blake2b(payload.encode("utf-8")).hexdigest()


# ── Public API ──────────────────────────────────────────────────────────────


async def init_audit_table(db_path: Optional[str] = None) -> None:
    """Idempotent ``CREATE TABLE``/``CREATE INDEX`` for ``hitl_audit_log``.

    Called once from the FastAPI lifespan startup, after ``init_dlq_table()``.
    """
    async with aiosqlite.connect(db_path or DB_CATALOG_PATH) as db:
        await db.execute(_AUDIT_DDL)
        # Additive migration: CREATE TABLE IF NOT EXISTS leaves a pre-existing
        # ledger untouched, so add the nullable project_id column when absent
        # (SQLite has no ADD COLUMN IF NOT EXISTS). Old rows read back as NULL.
        async with db.execute("PRAGMA table_info(hitl_audit_log)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "project_id" not in cols:
            await db.execute("ALTER TABLE hitl_audit_log ADD COLUMN project_id TEXT")
        await db.execute(_AUDIT_IDX)
        await db.execute(_AUDIT_PROJECT_IDX)
        await db.commit()


async def get_chain_head(
    session_id: str, db_path: Optional[str] = None
) -> Optional[str]:
    """Return the ``chain_hash`` of the most recent ``hitl_audit_log`` row for
    ``session_id``, or ``None`` if the session has no audit rows yet."""
    async with aiosqlite.connect(db_path or DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT chain_hash FROM hitl_audit_log WHERE session_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
    return None if row is None else str(row[0])


async def log_audit_event(
    *,
    session_id: str,
    action_description: str,
    proposed_content: Optional[str],
    resolution: str,
    resolution_comment: Optional[str] = None,
    audit_id: Optional[str] = None,
    project_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Append one immutable row to the HITL audit chain; return its ``chain_hash``.

    ``proposed_content`` is scrubbed before it is stored or hashed. The
    read-head → hash → INSERT section is serialised by :data:`_CHAIN_LOCK`.

    ``project_id`` scopes the row for the dashboard's per-project audit view. It is
    a plain filterable column and is deliberately **not** part of the chain hash
    (which covers ``session_id``), so tagging never alters chain verification.
    """
    path = db_path or DB_CATALOG_PATH
    aid = audit_id or uuid.uuid4().hex
    request_kind = _classify(action_description)
    scrubbed = SecretsScrubber.scrub(proposed_content or "")
    content_hash = hashlib.blake2b(scrubbed.encode("utf-8")).hexdigest()
    operator = os.getenv("AILIENANT_OPERATOR_EMAIL", "") or None
    resolved_at = int(time.time())

    async with _CHAIN_LOCK:
        prev = await get_chain_head(session_id, db_path=path)
        chain_hash = _compute_chain_hash(
            prev=prev,
            audit_id=aid,
            session_id=session_id,
            request_kind=request_kind,
            action_description=action_description,
            proposed_content_hash=content_hash,
            resolution=resolution,
            resolved_at=resolved_at,
        )
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "INSERT INTO hitl_audit_log (audit_id, session_id, "
                "request_kind, action_description, proposed_content_scrubbed, "
                "proposed_content_hash, resolution, resolution_comment, "
                "operator_user_email, prev_chain_hash, chain_hash, resolved_at, "
                "project_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, session_id, request_kind, action_description,
                    scrubbed, content_hash, resolution, resolution_comment,
                    operator, prev, chain_hash, resolved_at,
                    project_id or None,
                ),
            )
            await db.commit()

    logger.info(
        "HITL audit row %s logged: session=%s kind=%s resolution=%s",
        aid, session_id, request_kind, resolution,
    )
    return chain_hash


async def verify_chain(session_id: str, db_path: Optional[str] = None) -> bool:
    """Re-walk every ``hitl_audit_log`` row for ``session_id`` in insertion
    order, recompute each ``chain_hash``, and raise :class:`AuditChainBrokenError`
    on the first divergence (a tampered row, or a broken ``prev`` link).

    Returns ``True`` when the chain is intact (including the empty chain).
    """
    async with aiosqlite.connect(db_path or DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT audit_id, request_kind, action_description, "
            "proposed_content_hash, resolution, prev_chain_hash, chain_hash, "
            "resolved_at FROM hitl_audit_log WHERE session_id = ? "
            "ORDER BY rowid ASC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()

    expected_prev: Optional[str] = None
    for r in rows:
        stored_prev = None if r[5] is None else str(r[5])
        stored_chain = str(r[6])
        recomputed = _compute_chain_hash(
            prev=expected_prev,
            audit_id=str(r[0]),
            session_id=session_id,
            request_kind=str(r[1]),
            action_description=str(r[2]),
            proposed_content_hash=str(r[3]),
            resolution=str(r[4]),
            resolved_at=int(r[7]),
        )
        if stored_prev != expected_prev or stored_chain != recomputed:
            raise AuditChainBrokenError(
                state_head=recomputed,
                db_head=stored_chain,
                task_id=session_id,
            )
        expected_prev = stored_chain
    return True
