# core/janitor.py
"""Memory Janitor & GC.

Three cleanup targets:
    run_vector_gc          — delete LanceDB vectors whose source files no longer exist on disk
    purge_obsolete_graphs  — delete old pruned MCTS episodes from the MCTS audit DB
    purge_old_telemetry    — delete old rows from the append-only telemetry tables (DEBT-120)
    run_janitor            — orchestrator that calls all three and returns a combined JanitorReport
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional

import aiosqlite
from pydantic import BaseModel

from shared.config import MCTS_DB_PATH
from core.storage_paths import graphrag_lancedb_path, project_id_for

logger = logging.getLogger("JANITOR")

_WORKSPACE_EMBEDDINGS_TABLE: str = "workspace_embeddings"
# Per-symbol chunk vectors live in a sibling table and orphan on the same event
# (their source file disappearing), so GC must sweep both or deleted files keep
# contributing chunk evidence to retrieval indefinitely.
_SYMBOL_CHUNKS_TABLE: str = "symbol_chunk_embeddings"
_VECTOR_TABLES: tuple[str, ...] = (_WORKSPACE_EMBEDDINGS_TABLE, _SYMBOL_CHUNKS_TABLE)
_DEFAULT_RETENTION_DAYS: int = 30


# ── Report models (Pydantic so FastAPI can serialise them directly) ────────────

class VectorGCReport(BaseModel):
    orphaned_paths: List[str]
    deleted_count: int


class GraphGCReport(BaseModel):
    purged_count: int


class TelemetryGCReport(BaseModel):
    request_latency_purged: int
    container_lifecycle_purged: int
    action_token_usage_purged: int
    tool_invocations_purged: int


class JanitorReport(BaseModel):
    vector_gc: VectorGCReport
    graph_gc: GraphGCReport
    telemetry_gc: TelemetryGCReport


# ── Internal helpers ───────────────────────────────────────────────────────────

def _vector_gc_sync(workspace_root: str, lancedb_path: str) -> VectorGCReport:
    """Sync implementation; always called via asyncio.to_thread()."""
    import lancedb
    import pyarrow.compute as pc

    ws_hash: str = project_id_for(workspace_root)
    db = lancedb.connect(lancedb_path)
    present = db.table_names()
    if _WORKSPACE_EMBEDDINGS_TABLE not in present and _SYMBOL_CHUNKS_TABLE not in present:
        logger.info("Janitor: no vector tables found — skipping vector GC.")
        return VectorGCReport(orphaned_paths=[], deleted_count=0)

    # A file's orphan status is a property of the filesystem, not of any one
    # table, so paths are unioned across both stores before the existence check —
    # a file may have chunk rows in one and a stale file-level row in the other.
    tables = {name: db.open_table(name) for name in _VECTOR_TABLES if name in present}
    unique_paths: set[str] = set()
    for tbl in tables.values():
        arrow_table = tbl.to_lance().to_table(columns=["file_path", "workspace_hash"])
        mask = pc.equal(arrow_table.column("workspace_hash"), ws_hash)  # pyright: ignore[reportAttributeAccessIssue] — pyarrow.compute stub omits equal
        unique_paths.update(arrow_table.filter(mask).column("file_path").to_pylist())

    orphaned: List[str] = [p for p in sorted(unique_paths) if not os.path.exists(p)]

    for file_path in orphaned:
        safe_path: str = file_path.replace("'", "''")
        predicate = f"workspace_hash = '{ws_hash}' AND file_path = '{safe_path}'"
        for tbl in tables.values():
            tbl.delete(predicate)
        logger.info("Janitor: deleted orphaned vector for %s", file_path)

    if orphaned:
        logger.info(
            "Janitor: vector GC complete — %d orphaned vectors deleted (workspace=%s..)",
            len(orphaned), ws_hash[:8],
        )
    return VectorGCReport(orphaned_paths=orphaned, deleted_count=len(orphaned))


# ── Public async API ───────────────────────────────────────────────────────────

async def run_vector_gc(
    workspace_root: str,
    lancedb_path: Optional[str] = None,
) -> VectorGCReport:
    """Query LanceDB workspace_embeddings, delete rows whose file_path no longer exists.

    The GraphRAG store is partitioned per project, so the path defaults to the
    bound project's directory when no explicit path is supplied.
    LanceDB is synchronous; wrapped in asyncio.to_thread() for non-blocking operation.
    """
    resolved_path = lancedb_path or graphrag_lancedb_path()
    return await asyncio.to_thread(_vector_gc_sync, workspace_root, resolved_path)


async def purge_obsolete_graphs(
    mcts_db_path: str = MCTS_DB_PATH,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> GraphGCReport:
    """Delete pruned MCTS episodes older than retention_days from the MCTS audit DB.

    Only rows with prune_reason IS NOT NULL are candidates — stable nodes are preserved.
    """
    threshold: float = time.time() - retention_days * 86400.0
    async with aiosqlite.connect(mcts_db_path) as db:
        cur = await db.execute(
            "DELETE FROM mcts_episodes WHERE prune_reason IS NOT NULL AND accepted_at < ?",
            (threshold,),
        )
        await db.commit()
        purged: int = cur.rowcount if cur.rowcount is not None else 0
    logger.info(
        "Janitor: graph GC complete — %d pruned MCTS episodes deleted (retention=%dd).",
        purged, retention_days,
    )
    return GraphGCReport(purged_count=purged)


async def purge_old_telemetry(retention_days: int = _DEFAULT_RETENTION_DAYS) -> TelemetryGCReport:
    """Delete old rows from the three append-only telemetry tables (DEBT-120).

    ``core.telemetry`` owns the actual DB handle and its ``threading.Lock`` (a
    plain ``sqlite3.Connection``, not aiosqlite), so the delete runs there and
    is offloaded to a worker thread here — the same pattern ``run_vector_gc``
    uses for LanceDB's synchronous API.
    """
    from core.telemetry import purge_old_telemetry as _purge_sync

    deleted = await asyncio.to_thread(_purge_sync, retention_days)
    return TelemetryGCReport(
        request_latency_purged=deleted["request_latency"],
        container_lifecycle_purged=deleted["container_lifecycle"],
        action_token_usage_purged=deleted["action_token_usage"],
        tool_invocations_purged=deleted["tool_invocations"],
    )


async def run_janitor(
    workspace_root: str,
    lancedb_path: Optional[str] = None,
    mcts_db_path: str = MCTS_DB_PATH,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> JanitorReport:
    """Orchestrate all three GC passes and return a combined JanitorReport."""
    vector_report = await run_vector_gc(workspace_root, lancedb_path)
    graph_report = await purge_obsolete_graphs(mcts_db_path, retention_days)
    telemetry_report = await purge_old_telemetry(retention_days)
    logger.info(
        "Janitor run complete: vectors_deleted=%d graphs_purged=%d telemetry_purged=%d",
        vector_report.deleted_count,
        graph_report.purged_count,
        telemetry_report.request_latency_purged
        + telemetry_report.container_lifecycle_purged
        + telemetry_report.action_token_usage_purged
        + telemetry_report.tool_invocations_purged,
    )
    return JanitorReport(vector_gc=vector_report, graph_gc=graph_report, telemetry_gc=telemetry_report)
