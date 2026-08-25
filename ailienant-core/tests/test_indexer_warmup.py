# ailienant-core/tests/test_indexer_warmup.py
#
# Focused async tests for the warm-up indexing gate.
# DoD: sub-threshold count defers the full crawl; at-threshold count runs it.
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.websocket_manager as ws_mod
import core.indexer as indexer_mod
from core.indexer import LazyIndexer, _WARMUP_MIN_FILES
from shared.contracts import IndexingResult, SymbolDef


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_fake_paths(n: int) -> list[str]:
    return [f"/workspace/file_{i}.py" for i in range(n)]


def _make_vfs_mock_ok(content: str = "def f():\n    pass\n") -> MagicMock:
    """VFSMiddleware instance whose read_safe always returns ok content."""
    vfs_cls = MagicMock()
    vfs_inst = MagicMock()
    result = MagicMock()
    result.ok = True
    result.content = content
    result.error = None
    vfs_inst.read_safe.return_value = result
    vfs_cls.return_value = vfs_inst
    return vfs_cls


def _make_vfs_mock_not_ok() -> MagicMock:
    """VFSMiddleware instance whose read_safe always returns a not-ok result."""
    vfs_cls = MagicMock()
    vfs_inst = MagicMock()
    result = MagicMock()
    result.ok = False
    result.content = None
    result.error = "stub — not-ok"
    vfs_inst.read_safe.return_value = result
    vfs_cls.return_value = vfs_inst
    return vfs_cls


@pytest.fixture
def mock_vfs_manager() -> MagicMock:
    mock = MagicMock()
    mock.broadcast_indexing_complete = AsyncMock()
    mock.broadcast_indexing_progress = AsyncMock()
    mock.broadcast_indexing_error = AsyncMock()
    return mock


@pytest.mark.anyio
async def test_sub_threshold_defers_full_crawl(mock_vfs_manager: MagicMock) -> None:
    """When eligible files < _WARMUP_MIN_FILES, the full crawl is skipped."""
    indexer = LazyIndexer()
    n_files = _WARMUP_MIN_FILES - 1  # 4 — below threshold

    mock_pool_run = AsyncMock()

    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=_make_fake_paths(n_files)),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch.object(indexer_mod.compute_pool, "run", mock_pool_run),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/workspace", "proj_001", "sess_001")

    # Warm-up path: no batch processing, session complete signal fires, but _is_complete stays False
    mock_pool_run.assert_not_called()
    mock_vfs_manager.broadcast_indexing_complete.assert_awaited_once()
    assert indexer._is_complete is False  # next session can retry when workspace grows


@pytest.mark.anyio
async def test_at_threshold_runs_full_crawl(mock_vfs_manager: MagicMock) -> None:
    """When eligible files == _WARMUP_MIN_FILES, the full crawl runs to completion."""
    indexer = LazyIndexer()
    n_files = _WARMUP_MIN_FILES  # 5 — at threshold

    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=_make_fake_paths(n_files)),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch("core.vfs_middleware.VFSMiddleware", _make_vfs_mock_not_ok()),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/workspace", "proj_001", "sess_001")

    # Full crawl path: crawl completes (all files VFS-skipped, but loop ran)
    mock_vfs_manager.broadcast_indexing_complete.assert_awaited_once()
    assert indexer._is_complete is True  # crawl marked done


@pytest.mark.anyio
async def test_full_crawl_populates_symbol_catalog(mock_vfs_manager: MagicMock) -> None:
    """DEBT-147: a successfully indexed file during the bulk crawl must write its
    symbols into the same catalog the reactive (per-save) path populates —
    otherwise a freshly cold-indexed workspace has an empty symbol catalog
    until every file is individually re-saved."""
    indexer = LazyIndexer()
    n_files = _WARMUP_MIN_FILES
    paths = _make_fake_paths(n_files)

    symbols = [SymbolDef(qualified_name="f", kind="function", start_line=1, end_line=2)]
    fake_result = IndexingResult(
        file_path=paths[0], symbol_count=len(symbols), language_id="python",
        success=True, imports=[], symbols=symbols,
    )
    upsert_symbols = AsyncMock()
    fake_semantic_mgr = MagicMock()
    fake_semantic_mgr.semantic_upsert = AsyncMock()

    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=paths),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch.object(indexer_mod.compute_pool, "run", AsyncMock(return_value=fake_result)),
        patch.object(indexer_mod, "upsert_symbol_definitions", upsert_symbols),
        patch("core.vfs_middleware.VFSMiddleware", _make_vfs_mock_ok()),
        patch("core.memory.semantic_memory.SemanticMemoryManager", return_value=fake_semantic_mgr),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/workspace", "proj_001", "sess_001")

    assert upsert_symbols.await_count == n_files
    upsert_symbols.assert_any_await(paths[0], symbols, "proj_001")


@pytest.mark.anyio
async def test_full_crawl_survives_symbol_catalog_write_failure(mock_vfs_manager: MagicMock) -> None:
    """A failed symbol-catalog write is advisory-only — it must never abort the
    crawl or block the canonical index/embed flow for that file."""
    indexer = LazyIndexer()
    paths = _make_fake_paths(_WARMUP_MIN_FILES)
    fake_result = IndexingResult(
        file_path=paths[0], symbol_count=0, language_id="python",
        success=True, imports=[], symbols=[],
    )
    fake_semantic_mgr = MagicMock()
    fake_semantic_mgr.semantic_upsert = AsyncMock()

    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=paths),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch.object(indexer_mod.compute_pool, "run", AsyncMock(return_value=fake_result)),
        patch.object(
            indexer_mod, "upsert_symbol_definitions",
            AsyncMock(side_effect=RuntimeError("catalog db locked")),
        ),
        patch("core.vfs_middleware.VFSMiddleware", _make_vfs_mock_ok()),
        patch("core.memory.semantic_memory.SemanticMemoryManager", return_value=fake_semantic_mgr),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/workspace", "proj_001", "sess_001")

    mock_vfs_manager.broadcast_indexing_complete.assert_awaited_once()
    assert indexer._is_complete is True
