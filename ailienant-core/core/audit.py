"""Phase 6.5 — minimal HITL audit-chain seam.

This is a deliberately minimal stub. Phase 6.6 replaces it with the full
``AuditLogger`` (``log_request`` / ``log_resolution`` / ``verify_chain``) backed
by the ``hitl_audit_log`` table and the blake2b chain formula (Blueprint §7).

Until then, :func:`get_chain_head` always returns ``None`` (no chain exists),
which makes the Supervisor's chain-verify trigger a typed, load-bearing no-op:
the divergence check only fires once ``state["hitl_audit_chain_head"]`` is set
by a real audit row — impossible before Phase 6.6 — so the stub is safe.

Blueprint reference: §7 (Append-Only HITL Audit Chain).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


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


async def get_chain_head(session_id: str) -> Optional[str]:
    """Return the blake2b ``chain_hash`` of the last resolved ``hitl_audit_log``
    row for ``session_id``.

    Phase 6.5 stub — no audit table exists yet, so there is no chain and the
    head is ``None``. Phase 6.6 implements the real ``SELECT … ORDER BY
    requested_at DESC LIMIT 1`` query.
    """
    return None
