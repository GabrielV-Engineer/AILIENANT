# core/janitor.py
"""Memory Janitor & GC.

Two cleanup targets:
    run_vector_gc          — delete LanceDB vectors whose source files no longer exist on disk
    purge_obsolete_graphs  — delete old pruned MCTS episodes from the MCTS audit DB
    run_janitor            — orchestrator that calls both and returns a combined JanitorReport
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional

import aiosqlite
import lancedb
import pyarrow.compute as pc
from pydantic import BaseModel

from shared.config import MCTS_DB_PATH
from core.storage_paths import graphrag_lancedb_path, project_id_for

logger = logging.getLogger("JANITOR")

_WORKSPACE_EMBEDDINGS_TABLE: str = "workspace_embeddings"
_DEFAULT_RETENTION_DAYS: int = 30


# ── Report models (Pydantic so FastAPI can serialise them directly) ────────────

class VectorGCReport(BaseModel):
    orphaned_paths: List[str]
    deleted_count: int


class GraphGCReport(BaseModel):
    purged_count: int


class JanitorReport(BaseModel):
    vector_gc: VectorGCReport
    graph_gc: GraphGCReport


# ── Internal helpers ───────────────────────────────────────────────────────────

def _vector_gc_sync(workspace_root: str, lancedb_path: str) -> VectorGCReport:
    """Sync implementation; always called via asyncio.to_thread()."""
    ws_hash: str = project_id_for(workspace_root)
    db = lancedb.connect(lancedb_path)
    if _WORKSPACE_EMBEDDINGS_TABLE not in db.table_names():
        logger.info("Janitor: table '%s' not found — skipping vector GC.", _WORKSPACE_EMBEDDINGS_TABLE)
        return VectorGCReport(orphaned_paths=[], deleted_count=0)

    tbl = db.open_table(_WORKSPACE_EMBEDDINGS_TABLE)
    arrow_table = tbl.to_lance().to_table(columns=["file_path", "workspace_hash"])
    mask = pc.equal(arrow_table.column("workspace_hash"), ws_hash)  # pyright: ignore[reportAttributeAccessIssue] — pyarrow.compute stub omits equal
    ws_table = arrow_table.filter(mask)

    unique_paths: List[str] = list(set(ws_table.column("file_path").to_pylist()))
    orphaned: List[str] = [p for p in unique_paths if not os.path.exists(p)]

    for file_path in orphaned:
        safe_path: str = file_path.replace("'", "''")
        tbl.delete(f"workspace_hash = '{ws_hash}' AND file_path = '{safe_path}'")
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


async def run_janitor(
    workspace_root: str,
    lancedb_path: Optional[str] = None,
    mcts_db_path: str = MCTS_DB_PATH,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> JanitorReport:
    """Orchestrate both GC passes and return a combined JanitorReport."""
    vector_report = await run_vector_gc(workspace_root, lancedb_path)
    graph_report = await purge_obsolete_graphs(mcts_db_path, retention_days)
    logger.info(
        "Janitor run complete: vectors_deleted=%d graphs_purged=%d",
        vector_report.deleted_count,
        graph_report.purged_count,
    )
    return JanitorReport(vector_gc=vector_report, graph_gc=graph_report)
