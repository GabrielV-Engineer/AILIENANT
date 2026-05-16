# tests/test_janitor.py
"""Phase 3.5 DoD — Memory Janitor: orphaned vector GC + obsolete MCTS graph purge."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pytest

from core.janitor import (
    GraphGCReport,
    JanitorReport,
    VectorGCReport,
    _vector_gc_sync,
    purge_obsolete_graphs,
    run_janitor,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_arrow_table(rows: list[dict[str, str]]) -> pa.Table:
    """Build a minimal PyArrow table mimicking the workspace_embeddings schema."""
    return pa.table({
        "file_path": pa.array([r["file_path"] for r in rows], type=pa.utf8()),
        "workspace_hash": pa.array([r["workspace_hash"] for r in rows], type=pa.utf8()),
    })


def _ws_hash(ws: str) -> str:
    import hashlib
    return hashlib.sha256(ws.encode("utf-8")).hexdigest()


# ── Test 1: orphaned file → delete called ─────────────────────────────────────

def test_vector_gc_deletes_orphaned_file() -> None:
    ws = "/my/workspace"
    ws_h = _ws_hash(ws)
    missing_path = "/my/workspace/src/gone.py"

    arrow_tbl = _make_arrow_table([{"file_path": missing_path, "workspace_hash": ws_h}])
    mock_lance_ds = MagicMock()
    mock_lance_ds.to_table.return_value = arrow_tbl
    mock_tbl = MagicMock()
    mock_tbl.to_lance.return_value = mock_lance_ds
    mock_db = MagicMock()
    mock_db.table_names.return_value = ["workspace_embeddings"]
    mock_db.open_table.return_value = mock_tbl

    with patch("core.janitor.lancedb") as mock_lancedb, \
         patch("core.janitor.os.path.exists", return_value=False):
        mock_lancedb.connect.return_value = mock_db
        report = _vector_gc_sync(ws, "/fake/lancedb")

    assert report.deleted_count == 1
    assert missing_path in report.orphaned_paths
    mock_tbl.delete.assert_called_once()
    delete_arg: str = mock_tbl.delete.call_args[0][0]
    assert "gone.py" in delete_arg
    assert ws_h in delete_arg


# ── Test 2: existing file → delete NOT called ─────────────────────────────────

def test_vector_gc_skips_existing_file() -> None:
    ws = "/my/workspace"
    ws_h = _ws_hash(ws)
    existing_path = "/my/workspace/src/main.py"

    arrow_tbl = _make_arrow_table([{"file_path": existing_path, "workspace_hash": ws_h}])
    mock_lance_ds = MagicMock()
    mock_lance_ds.to_table.return_value = arrow_tbl
    mock_tbl = MagicMock()
    mock_tbl.to_lance.return_value = mock_lance_ds
    mock_db = MagicMock()
    mock_db.table_names.return_value = ["workspace_embeddings"]
    mock_db.open_table.return_value = mock_tbl

    with patch("core.janitor.lancedb") as mock_lancedb, \
         patch("core.janitor.os.path.exists", return_value=True):
        mock_lancedb.connect.return_value = mock_db
        report = _vector_gc_sync(ws, "/fake/lancedb")

    assert report.deleted_count == 0
    assert report.orphaned_paths == []
    mock_tbl.delete.assert_not_called()


# ── Test 3: table missing → returns empty report, no exception ─────────────────

def test_vector_gc_handles_missing_table() -> None:
    mock_db = MagicMock()
    mock_db.table_names.return_value = []  # table absent

    with patch("core.janitor.lancedb") as mock_lancedb:
        mock_lancedb.connect.return_value = mock_db
        report = _vector_gc_sync("/some/ws", "/fake/lancedb")

    assert report.deleted_count == 0
    assert report.orphaned_paths == []
    mock_db.open_table.assert_not_called()


# ── Test 4: SQL injection prevention — single quotes in path are escaped ───────

def test_vector_gc_escapes_single_quotes_in_path() -> None:
    ws = "/my/workspace"
    ws_h = _ws_hash(ws)
    tricky_path = "/my/workspace/it's/file.py"

    arrow_tbl = _make_arrow_table([{"file_path": tricky_path, "workspace_hash": ws_h}])
    mock_lance_ds = MagicMock()
    mock_lance_ds.to_table.return_value = arrow_tbl
    mock_tbl = MagicMock()
    mock_tbl.to_lance.return_value = mock_lance_ds
    mock_db = MagicMock()
    mock_db.table_names.return_value = ["workspace_embeddings"]
    mock_db.open_table.return_value = mock_tbl

    with patch("core.janitor.lancedb") as mock_lancedb, \
         patch("core.janitor.os.path.exists", return_value=False):
        mock_lancedb.connect.return_value = mock_db
        report = _vector_gc_sync(ws, "/fake/lancedb")

    assert report.deleted_count == 1
    delete_arg: str = mock_tbl.delete.call_args[0][0]
    # Single quote in path must be doubled to prevent SQL injection
    assert "it''s" in delete_arg


# ── Test 5 (anyio): purge deletes old pruned MCTS nodes ──────────────────────

@pytest.mark.anyio
async def test_purge_obsolete_graphs_deletes_old_pruned() -> None:
    mock_cursor = AsyncMock()
    mock_cursor.rowcount = 5

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=mock_cursor)
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    with patch("core.janitor.aiosqlite.connect", return_value=mock_conn):
        report = await purge_obsolete_graphs(mcts_db_path="fake.sqlite", retention_days=30)

    assert report.purged_count == 5
    mock_conn.execute.assert_called_once()
    sql_call: str = mock_conn.execute.call_args[0][0]
    assert "DELETE FROM mcts_episodes" in sql_call
    assert "prune_reason IS NOT NULL" in sql_call
    assert "accepted_at < ?" in sql_call
    mock_conn.commit.assert_called_once()


# ── Test 6 (anyio): run_janitor calls both GC functions ──────────────────────

@pytest.mark.anyio
async def test_run_janitor_calls_both_gc_functions() -> None:
    fake_vector_report = VectorGCReport(orphaned_paths=["/old.py"], deleted_count=1)
    fake_graph_report = GraphGCReport(purged_count=7)

    with patch("core.janitor.run_vector_gc", return_value=fake_vector_report) as mock_vgc, \
         patch("core.janitor.purge_obsolete_graphs", return_value=fake_graph_report) as mock_gg:
        report = await run_janitor(
            workspace_root="/ws",
            lancedb_path="/fake/lancedb",
            mcts_db_path="fake.sqlite",
            retention_days=14,
        )

    mock_vgc.assert_called_once_with("/ws", "/fake/lancedb")
    mock_gg.assert_called_once_with("fake.sqlite", 14)
    assert isinstance(report, JanitorReport)
    assert report.vector_gc.deleted_count == 1
    assert report.graph_gc.purged_count == 7
