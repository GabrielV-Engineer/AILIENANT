import asyncio
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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
    # Phase 2.4: PPR GraphRAG foundation
    """CREATE TABLE IF NOT EXISTS dependency_graph (
        source_file        TEXT NOT NULL,
        target_dependency  TEXT NOT NULL,
        project_id         TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source_file, target_dependency, project_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dg_source ON dependency_graph(source_file, project_id)",
    """CREATE TABLE IF NOT EXISTS ppr_scores (
        file_path    TEXT NOT NULL,
        project_id   TEXT NOT NULL DEFAULT '',
        ppr_score    REAL NOT NULL DEFAULT 0.0,
        computed_at  REAL NOT NULL,
        PRIMARY KEY (file_path, project_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ppr_score ON ppr_scores(project_id, ppr_score DESC)",
    # Phase 2.5: indexed_files — tracks which files have been processed by LazyIndexer
    """CREATE TABLE IF NOT EXISTS indexed_files (
        file_path    TEXT NOT NULL,
        project_id   TEXT NOT NULL DEFAULT '',
        indexed_at   REAL NOT NULL,
        PRIMARY KEY (file_path, project_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_if_project ON indexed_files(project_id)",
    # Phase 7.9.A.7 — Command-menu config stores (concurrency-safe via WAL).
    # Entity collections live here (not settings.json) so concurrent CRUD from
    # independent routers cannot lose updates (last-writer-wins on a JSON file).
    """CREATE TABLE IF NOT EXISTS skills (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        body        TEXT NOT NULL,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS mcp_servers (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        transport   TEXT NOT NULL DEFAULT 'stdio',
        uri         TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS hooks (
        id          TEXT PRIMARY KEY,
        event       TEXT NOT NULL,
        command     TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_role_overrides (
        role          TEXT PRIMARY KEY,
        system_prompt TEXT NOT NULL,
        updated_at    REAL NOT NULL
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

# Additive graph-analytics columns. SQLite has no ``ADD COLUMN IF NOT EXISTS``,
# so each is applied only when absent (checked via PRAGMA table_info). All default
# to NULL, so pre-migration rows and readers that ignore the column keep working.
_COLUMN_MIGRATIONS: List[Tuple[str, str, str]] = [
    ("dependency_graph", "confidence", "TEXT"),
    ("dependency_graph", "confidence_score", "REAL"),
    ("ppr_scores", "leiden_community_id", "INTEGER"),
]


async def _apply_column_migrations(db: aiosqlite.Connection) -> None:
    """Add missing analytics columns to existing tables (idempotent)."""
    for table, column, decl in _COLUMN_MIGRATIONS:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


async def init_db() -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        for stmt in _DDL:
            await db.execute(stmt)
        await _apply_column_migrations(db)
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


# ── Phase 5.3 — Dependent-file lookup (inbound edges) ──────────────────────


async def get_dependents(target: str, project_id: str = "") -> List[str]:
    """Return files that import the given target (1-hop backward edge).

    Used by GetSymbolReferencesTool and TraceDataFlowTool (Phase 5.3).
    Deterministic ordering for caching: sorts by source_file ascending.
    """
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT source_file FROM dependency_graph "
            "WHERE target_dependency = ? AND project_id = ? "
            "ORDER BY source_file",
            (target, project_id),
        ) as cur:
            rows = await cur.fetchall()
            return [row[0] for row in rows]


# ── Per-project graph-write serialization ────────────────────────────────────
# The dependency graph and its PageRank scores have several concurrent writers:
# the human's reactive save re-index, manual memory consolidation, and the
# agent's apply_patch. Each runs a DELETE-then-INSERT that is not atomic across
# its awaits, so without serialization they interleave into phantom edges or
# corrupt PPR rankings. A per-project asyncio.Lock makes every graph/PPR write
# critical section mutually exclusive within one event loop.
#
# Keyed by (event-loop id, project_id): a lock must be bound to the loop that
# awaits it, so caching across loops (e.g. one asyncio.run per test) would bind
# a stale lock to a dead loop. Distinct projects never contend with each other.
_graph_write_locks: Dict[Tuple[int, str], "asyncio.Lock"] = {}
_graph_locks_guard = threading.Lock()


def graph_write_lock(project_id: str = "") -> "asyncio.Lock":
    """Return the per-project lock serializing dependency_graph + ppr_scores writes.

    The write helpers below acquire it internally. Read-side consumers (memory
    consolidation, the GraphRAG extractor) acquire it to snapshot the graph
    without a concurrent restructure tearing the read. The lock is NOT reentrant
    (``asyncio.Lock``): a holder must never call the self-locking write helpers
    while holding it — release first, then write.
    """
    loop = asyncio.get_running_loop()
    key = (id(loop), project_id)
    with _graph_locks_guard:
        lock = _graph_write_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _graph_write_locks[key] = lock
        return lock


# ── Phase 2.4: PPR GraphRAG ────────────────────────────────────────────────────

async def upsert_dependencies(
    source_file: str, imports: List[str], project_id: str = ""
) -> None:
    """Replace all outgoing dependency edges for source_file, then insert new ones."""
    async with graph_write_lock(project_id):
        async with aiosqlite.connect(DB_CATALOG_PATH) as db:
            await db.execute(
                "DELETE FROM dependency_graph WHERE source_file=? AND project_id=?",
                (source_file, project_id),
            )
            if imports:
                # Named columns: confidence/confidence_score are left NULL here and
                # refined by the batch analytics pass (a freshly parsed edge has no
                # global resolution context yet).
                await db.executemany(
                    "INSERT OR IGNORE INTO dependency_graph "
                    "(source_file, target_dependency, project_id) VALUES (?,?,?)",
                    [(source_file, imp, project_id) for imp in imports],
                )
            await db.commit()


async def get_all_edges(project_id: str = "") -> List[Tuple[str, str]]:
    """Return all (source_file, target_dependency) edges for a project."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT source_file, target_dependency "
            "FROM dependency_graph WHERE project_id=?",
            (project_id,),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]


async def upsert_ppr_scores(
    scores: Dict[str, float],
    project_id: str = "",
    communities: Optional[Dict[str, int]] = None,
) -> None:
    """Persist node centrality scores (and optional Louvain community ids) for the project.

    ``communities`` maps node → community id; when omitted, leiden_community_id is
    written NULL. Named columns so the additive column never shifts positionally.
    """
    now = time.time()
    comm = communities or {}
    async with graph_write_lock(project_id):
        async with aiosqlite.connect(DB_CATALOG_PATH) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO ppr_scores "
                "(file_path, project_id, ppr_score, computed_at, leiden_community_id) "
                "VALUES (?,?,?,?,?)",
                [
                    (path, project_id, score, now, comm.get(path))
                    for path, score in scores.items()
                ],
            )
            await db.commit()


async def upsert_edge_confidence(
    rows: List[Tuple[str, str, str, float]], project_id: str = ""
) -> None:
    """Refine confidence on existing edges (source, target, confidence, score).

    Computed by the batch analytics pass once the whole-graph resolution context
    is known. Runs under the graph write lock so it never races a restructure.
    """
    if not rows:
        return
    async with graph_write_lock(project_id):
        async with aiosqlite.connect(DB_CATALOG_PATH) as db:
            await db.executemany(
                "UPDATE dependency_graph SET confidence=?, confidence_score=? "
                "WHERE source_file=? AND target_dependency=? AND project_id=?",
                [(conf, score, src, tgt, project_id) for src, tgt, conf, score in rows],
            )
            await db.commit()


async def get_ppr_score(file_path: str, project_id: str = "") -> float:
    """Retrieve the stored PageRank score for a single file. Returns 0.0 if not yet computed."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT ppr_score FROM ppr_scores WHERE file_path=? AND project_id=?",
            (file_path, project_id),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0


# ── Phase 2.5: Indexed Files (LazyIndexer resume support) ─────────────────────

async def upsert_indexed_file(file_path: str, project_id: str = "") -> None:
    """Record that a file has been successfully processed by LazyIndexer."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO indexed_files VALUES (?,?,?)",
            (file_path, project_id, time.time()),
        )
        await db.commit()


async def get_indexed_count(project_id: str = "") -> int:
    """Return how many files have been indexed for a project."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM indexed_files WHERE project_id=?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def get_top_ppr_files(project_id: str = "", limit: int = 20) -> List[Tuple[str, float]]:
    """Return top-N (file_path, ppr_score) pairs ordered by score descending."""
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT file_path, ppr_score FROM ppr_scores "
            "WHERE project_id=? ORDER BY ppr_score DESC LIMIT ?",
            (project_id, limit),
        ) as cur:
            return [(r[0], float(r[1])) for r in await cur.fetchall()]


async def get_all_indexed_files() -> List[Tuple[str, str]]:
    """Return every (file_path, project_id) pair the LazyIndexer has recorded.

    Used by the Memory dashboard /sections endpoint to enumerate indexed folders
    without touching the vector store. Cheap — a single full scan of indexed_files.
    """
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT file_path, project_id FROM indexed_files"
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]


async def get_ppr_scores_bulk(
    file_paths: List[str], project_id: str = ""
) -> Dict[str, float]:
    """Batch-fetch PPR scores for many files in one project. Missing files omitted.

    Chunks the IN-clause to stay under SQLite's 999-variable limit. Used by the
    Memory dashboard /graph endpoint to size/color nodes by centrality.
    """
    if not file_paths:
        return {}
    scores: Dict[str, float] = {}
    chunk_size = 900  # leave headroom under SQLite's default SQLITE_MAX_VARIABLE_NUMBER
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        for start in range(0, len(file_paths), chunk_size):
            chunk = file_paths[start:start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT file_path, ppr_score FROM ppr_scores "
                f"WHERE project_id=? AND file_path IN ({placeholders})",
                (project_id, *chunk),
            ) as cur:
                for r in await cur.fetchall():
                    scores[r[0]] = float(r[1])
    return scores


async def get_community_ids_bulk(
    node_ids: List[str], project_id: str = ""
) -> Dict[str, int]:
    """Batch-fetch Louvain community ids for many nodes. Missing/NULL omitted.

    Chunked like get_ppr_scores_bulk to stay under SQLite's variable limit. Feeds
    the Memory dashboard /graph endpoint so nodes can be colored by community.
    """
    if not node_ids:
        return {}
    communities: Dict[str, int] = {}
    chunk_size = 900
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        for start in range(0, len(node_ids), chunk_size):
            chunk = node_ids[start:start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT file_path, leiden_community_id FROM ppr_scores "
                f"WHERE project_id=? AND leiden_community_id IS NOT NULL "
                f"AND file_path IN ({placeholders})",
                (project_id, *chunk),
            ) as cur:
                for r in await cur.fetchall():
                    communities[r[0]] = int(r[1])
    return communities


async def get_graph_edges_enriched(
    project_id: str = "",
) -> List[Tuple[str, str, Optional[str], Optional[float]]]:
    """Return all edges with their confidence label/score for one project.

    Parallel to get_all_edges (which stays 2-tuple for BFS/PPR consumers); this
    4-tuple form feeds the dashboard so edges can be styled by confidence.
    """
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT source_file, target_dependency, confidence, confidence_score "
            "FROM dependency_graph WHERE project_id=?",
            (project_id,),
        ) as cur:
            return [
                (r[0], r[1], r[2], float(r[3]) if r[3] is not None else None)
                for r in await cur.fetchall()
            ]


# ── Phase 2.1.13: Ghost Pruning (BranchSwitchHandler) ─────────────────────────

async def purge_file_nodes(filepath: str, project_id: str = "") -> None:
    """Remove all DB records for a deleted file (Ghost Pruning — Phase 2.1.13).

    Eradicates stale PPR scores, indexed_files entries, and outgoing dependency
    edges for the deleted file so they cannot pollute GraphRAG rankings or cause
    agent hallucinations after a branch switch.
    """
    async with graph_write_lock(project_id):
        async with aiosqlite.connect(DB_CATALOG_PATH) as db:
            await db.execute(
                "DELETE FROM ppr_scores WHERE file_path = ? AND project_id = ?",
                (filepath, project_id),
            )
            await db.execute(
                "DELETE FROM indexed_files WHERE file_path = ? AND project_id = ?",
                (filepath, project_id),
            )
            await db.execute(
                "DELETE FROM dependency_graph WHERE source_file = ? AND project_id = ?",
                (filepath, project_id),
            )
            await db.commit()


# ── Phase 2.2.B: WAL Safety ───────────────────────────────────────────────────

# ── Phase 7.9.A.7 — Command-menu config CRUD ─────────────────────────────────
# All entity collections (skills, mcp_servers, hooks, agent_role_overrides) are
# persisted here rather than in settings.json so concurrent writes from separate
# routers are serialized by the WAL engine instead of clobbering a JSON file.


async def list_skills() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, body, created_at, updated_at FROM skills ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def upsert_skill(skill_id: str, name: str, body: str) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT INTO skills (id, name, body, created_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, body=excluded.body, "
            "updated_at=excluded.updated_at",
            (skill_id, name, body, now, now),
        )
        await db.commit()


async def delete_skill(skill_id: str) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        await db.commit()


async def list_mcp_servers() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, transport, uri, enabled, created_at, updated_at "
            "FROM mcp_servers ORDER BY name"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    return rows


async def upsert_mcp_server(
    server_id: str, name: str, uri: str, transport: str = "stdio", enabled: bool = True
) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT INTO mcp_servers (id, name, transport, uri, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, transport=excluded.transport, "
            "uri=excluded.uri, enabled=excluded.enabled, updated_at=excluded.updated_at",
            (server_id, name, transport, uri, int(enabled), now, now),
        )
        await db.commit()


async def delete_mcp_server(server_id: str) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        await db.commit()


async def list_hooks() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, event, command, enabled, created_at FROM hooks ORDER BY created_at"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    return rows


async def upsert_hook(
    hook_id: str, event: str, command: str, enabled: bool = True
) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT INTO hooks (id, event, command, enabled, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET event=excluded.event, command=excluded.command, "
            "enabled=excluded.enabled",
            (hook_id, event, command, int(enabled), time.time()),
        )
        await db.commit()


async def delete_hook(hook_id: str) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute("DELETE FROM hooks WHERE id=?", (hook_id,))
        await db.commit()


async def list_agent_overrides() -> Dict[str, str]:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        async with db.execute(
            "SELECT role, system_prompt FROM agent_role_overrides"
        ) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}


async def upsert_agent_override(role: str, system_prompt: str) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute(
            "INSERT INTO agent_role_overrides (role, system_prompt, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(role) DO UPDATE SET system_prompt=excluded.system_prompt, "
            "updated_at=excluded.updated_at",
            (role, system_prompt, time.time()),
        )
        await db.commit()


async def delete_agent_override(role: str) -> None:
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute("DELETE FROM agent_role_overrides WHERE role=?", (role,))
        await db.commit()


async def wal_checkpoint() -> None:
    """Force PRAGMA wal_checkpoint(TRUNCATE) on the catalog DB.

    Called from the lifespan shutdown sequence so the catalog WAL file is
    truncated alongside the state checkpoint WAL. Without this, the catalog
    WAL can grow unbounded across restarts when the process is interrupted
    before the periodic WALCheckpointer fires.
    """
    async with aiosqlite.connect(DB_CATALOG_PATH) as db:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await db.commit()
