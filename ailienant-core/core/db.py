import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

import aiosqlite

from shared.config import DB_CATALOG_PATH

# ── Schema ──────────────────────────────────────────────────────────────────

_DDL = [
    "PRAGMA journal_mode=WAL",
    """CREATE TABLE IF NOT EXISTS session_state (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT    NOT NULL,
        file_path   TEXT    NOT NULL,
        timestamp   REAL    NOT NULL,
        version_id  TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ss_session ON session_state(session_id)",
    """CREATE TABLE IF NOT EXISTS tool_registry (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    UNIQUE NOT NULL,
        description   TEXT    NOT NULL,
        json_schema   TEXT    NOT NULL,
        mcp_privilege INTEGER NOT NULL DEFAULT 0
    )""",
]

# ── Sync connection (for use from VFSMiddleware.read_safe) ───────────────────

_sync_conn: Optional[sqlite3.Connection] = None
_sync_lock = threading.Lock()


def _get_sync_conn() -> sqlite3.Connection:
    global _sync_conn
    with _sync_lock:
        if _sync_conn is None:
            _sync_conn = sqlite3.connect(DB_CATALOG_PATH, check_same_thread=False)
            _sync_conn.execute("PRAGMA journal_mode=WAL")
        return _sync_conn


def log_file_read_sync(session_id: str, file_path: str, version_id: Optional[str]) -> None:
    conn = _get_sync_conn()
    conn.execute(
        "INSERT INTO session_state (session_id, file_path, timestamp, version_id) VALUES (?,?,?,?)",
        (session_id, file_path, time.time(), version_id),
    )
    conn.commit()


# ── Async API (for FastAPI endpoints) ───────────────────────────────────────

async def init_db() -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        for stmt in _DDL:
            await db.execute(stmt)
        await db.commit()
    _get_sync_conn()  # warm up sync connection too


async def get_session_reads(session_id: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT session_id, file_path, timestamp, version_id "
            "FROM session_state WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def register_tool(
    name: str, description: str, json_schema: str, mcp_privilege: bool = False
) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO tool_registry (name, description, json_schema, mcp_privilege) "
            "VALUES (?,?,?,?)",
            (name, description, json_schema, int(mcp_privilege)),
        )
        await db.commit()


async def get_tool(name: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, description, json_schema, mcp_privilege "
            "FROM tool_registry WHERE name=?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_tools() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, description, json_schema, mcp_privilege "
            "FROM tool_registry ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
