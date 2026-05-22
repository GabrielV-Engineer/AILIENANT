import aiosqlite
from fastapi import APIRouter

from core.audit import AuditChainBrokenError, verify_chain
from shared.config import DB_CATALOG_PATH

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _resolution_to_bool(r: str) -> bool | None:
    if r == "approved":
        return True
    if r == "rejected":
        return False
    return None  # "timeout" or future values


@router.get("/log")
async def get_audit_log(offset: int = 0, limit: int = 20) -> list[dict]:
    limit = min(limit, 100)
    async with aiosqlite.connect(f"file:{DB_CATALOG_PATH}?mode=ro", uri=True) as db:
        async with db.execute(
            "SELECT rowid, session_id, request_kind, resolution, "
            "chain_hash, prev_chain_hash, resolved_at "
            "FROM hitl_audit_log ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "session_id": r[1],
            "action_type": r[2],
            "approved": _resolution_to_bool(r[3]),
            "chain_hash": r[4],
            "prev_hash": r[5],
            "timestamp": str(r[6]),
        }
        for r in rows
    ]


@router.get("/stats")
async def get_audit_stats() -> dict:
    async with aiosqlite.connect(f"file:{DB_CATALOG_PATH}?mode=ro", uri=True) as db:
        async with db.execute("SELECT COUNT(*) FROM hitl_audit_log") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT resolution, COUNT(*) FROM hitl_audit_log GROUP BY resolution"
        ) as cur:
            by_res = {r[0]: r[1] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT request_kind, COUNT(*) FROM hitl_audit_log GROUP BY request_kind"
        ) as cur:
            by_type = {r[0]: r[1] for r in await cur.fetchall()}
    # DB stores "timeout" for no-human-decision (NOT NULL). Guard covers both
    # "timeout" (current) and any future "pending" literal.
    return {
        "total": total,
        "by_resolution": {
            "approved": by_res.get("approved", 0),
            "rejected": by_res.get("rejected", 0),
            "pending":  by_res.get("pending", 0) + by_res.get("timeout", 0),
        },
        "by_type": by_type,
    }


@router.get("/verify")
async def verify_audit_chain() -> dict:
    async with aiosqlite.connect(f"file:{DB_CATALOG_PATH}?mode=ro", uri=True) as db:
        async with db.execute(
            "SELECT DISTINCT session_id FROM hitl_audit_log"
        ) as cur:
            sessions = [r[0] for r in await cur.fetchall()]

    checked = 0
    for sid in sessions:
        try:
            await verify_chain(sid)
        except AuditChainBrokenError:
            return {"valid": False, "checked": checked, "error": "Tamper detected"}
        checked += 1

    return {"valid": True, "checked": len(sessions)}
