# ailienant-core/tests/test_indexer_warmup.py
#
# Focused async tests for the warm-up indexing gate.
# DoD: sub-threshold count defers the full crawl; at-threshold count runs it.
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.websocket_manager as ws_mod
import core.indexer as indexer_mod
from core.indexer import LazyIndexer, _WARMUP_MIN_FILES


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_fake_paths(n: int) -> list[str]:
    return [f"/workspace/file_{i}.py" for i in range(n)]


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
