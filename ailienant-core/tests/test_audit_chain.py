"""Phase 6.6 — HITL audit chain: integrity (E1) + tamper detection (E2).

Async audit functions are driven via ``asyncio.run`` so the suite needs no
``pytest-asyncio`` dependency. Each test isolates its DB in a ``tmp_path`` file
passed through the ``db_path=`` seam.
"""
import asyncio
import sqlite3

import pytest

from core.audit import (
    AuditChainBrokenError,
    _compute_chain_hash,
    get_chain_head,
    init_audit_table,
    log_audit_event,
    verify_chain,
)


def _db(tmp_path) -> str:
    return str(tmp_path / "audit_test.sqlite")


def test_chain_integrity_e1(tmp_path) -> None:
    """Three sequential events form a verifiable blake2b chain."""
    db = _db(tmp_path)

    async def _run():
        await init_audit_table(db_path=db)
        h1 = await log_audit_event(
            session_id="s1", action_description="BUDGET_OVERFLOW breach",
            proposed_content="cost report", resolution="approved", db_path=db,
        )
        h2 = await log_audit_event(
            session_id="s1", action_description="TOKEN_SPIKE turn",
            proposed_content="spike report", resolution="rejected", db_path=db,
        )
        h3 = await log_audit_event(
            session_id="s1", action_description="plain event",
            proposed_content=None, resolution="timeout", db_path=db,
        )
        head = await get_chain_head("s1", db_path=db)
        intact = await verify_chain("s1", db_path=db)
        return h1, h2, h3, head, intact

    h1, h2, h3, head, intact = asyncio.run(_run())
    assert head == h3
    assert intact is True

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT audit_id, request_kind, action_description, "
        "proposed_content_hash, resolution, prev_chain_hash, chain_hash, "
        "resolved_at FROM hitl_audit_log ORDER BY rowid ASC"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    assert [r[6] for r in rows] == [h1, h2, h3]
    assert [r[1] for r in rows] == ["BUDGET_OVERFLOW", "TOKEN_SPIKE", "OTHER"]

    # Manual recompute — each chain_hash must derive from the prior link.
    prev = None
    for r in rows:
        recomputed = _compute_chain_hash(
            prev=prev, audit_id=r[0], session_id="s1", request_kind=r[1],
            action_description=r[2], proposed_content_hash=r[3],
            resolution=r[4], resolved_at=r[7],
        )
        assert r[5] == prev          # stored prev_chain_hash links correctly
        assert r[6] == recomputed    # stored chain_hash matches the formula
        prev = r[6]


def test_tamper_detection_e2(tmp_path) -> None:
    """An out-of-band UPDATE of a historical row is detected by verify_chain."""
    db = _db(tmp_path)

    async def _seed():
        await init_audit_table(db_path=db)
        await log_audit_event(
            session_id="s1", action_description="BUDGET_OVERFLOW",
            proposed_content="x", resolution="approved", db_path=db,
        )
        await log_audit_event(
            session_id="s1", action_description="TOKEN_SPIKE",
            proposed_content="y", resolution="rejected", db_path=db,
        )

    asyncio.run(_seed())

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE hitl_audit_log SET action_description = 'HACKED' WHERE rowid = 2"
    )
    conn.commit()
    conn.close()

    with pytest.raises(AuditChainBrokenError):
        asyncio.run(verify_chain("s1", db_path=db))


def test_resolution_coverage(tmp_path) -> None:
    """approved, rejected and timeout each append a row (no gap-attack surface)."""
    db = _db(tmp_path)

    async def _run():
        await init_audit_table(db_path=db)
        for desc, res in (("a", "approved"), ("b", "rejected"), ("c", "timeout")):
            await log_audit_event(
                session_id="s2", action_description=desc,
                proposed_content=None, resolution=res, db_path=db,
            )

    asyncio.run(_run())

    conn = sqlite3.connect(db)
    resolutions = [
        row[0] for row in conn.execute(
            "SELECT resolution FROM hitl_audit_log WHERE session_id = 's2' "
            "ORDER BY rowid ASC"
        )
    ]
    conn.close()
    assert resolutions == ["approved", "rejected", "timeout"]
