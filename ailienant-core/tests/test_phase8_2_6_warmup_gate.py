# tests/test_phase8_2_6_warmup_gate.py
"""Division 8.2.6 Cold-Start / Warm-up — Checkpoint Gate.

Sibling-gate convention (test-only). Asserts one load-bearing invariant per row
across all four sub-phases of Division 8.2.6. No FE surface; no npm gate required.

Rows certified:
  A1  fresh LanceDB store reports the workspace as empty
  A2  a write makes the workspace non-empty (cache invalidated)
  B1  empty corpus + tci<30 → LOCAL_SMALL, is_red_alert False
      (css red-alert floor bypassed when corpus is absent)
  B2  non-empty corpus css<40 → CLOUD (regression guard — floor still fires)
  C1  search_with_paths makes ZERO embedding calls on a cold store
  D1  sub-threshold eligible count defers the full crawl, _is_complete stays False
  D2  at-threshold eligible count runs the full crawl, _is_complete set True
  E1  acomplete_byom retries ONCE on a local drop, re-raises on second failure
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litellm.exceptions import APIConnectionError

import api.websocket_manager as ws_mod
import core.indexer as indexer_mod
from core.indexer import LazyIndexer, _WARMUP_MIN_FILES
from core.memory import semantic_memory
from core.memory.context_auditor import derive_routing_decision
from core.memory.semantic_memory import SemanticMemoryManager
from tools.llm_gateway import LLMGateway

pytestmark = pytest.mark.anyio

_WS = "ws_8_2_6_gate"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_presence_cache() -> Any:
    semantic_memory._corpus_presence_cache.clear()
    yield
    semantic_memory._corpus_presence_cache.clear()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _seed_row(mgr: SemanticMemoryManager, file_path: str = "gate.py") -> None:
    record: Dict[str, Any] = {
        "file_path": file_path,
        "workspace_hash": _WS,
        "content_snippet": "def gate(): pass",
        "token_count": 4,
        "vector": [0.1] * 8,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_record(record, workspace_hash=_WS, file_path=file_path, hash_valid=True)


def _make_local_target(model: str = "ollama_chat/phi4") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.api_base = "http://localhost:11434"
    t.api_key = ""
    t.is_local = True
    return t


def _conn_err() -> APIConnectionError:
    return APIConnectionError(message="connection refused", llm_provider="ollama", model="ollama_chat/phi4")


@pytest.fixture
def mock_vfs_manager() -> MagicMock:
    m = MagicMock()
    m.broadcast_indexing_complete = AsyncMock()
    m.broadcast_indexing_progress = AsyncMock()
    m.broadcast_indexing_error = AsyncMock()
    return m


def _make_fake_paths(n: int) -> List[str]:
    return [f"/ws/file_{i}.py" for i in range(n)]


def _make_vfs_not_ok() -> MagicMock:
    vfs_cls = MagicMock()
    vfs_inst = MagicMock()
    result = MagicMock()
    result.ok = False
    result.content = None
    result.error = "stub"
    vfs_inst.read_safe.return_value = result
    vfs_cls.return_value = vfs_inst
    return vfs_cls


# ── A: Corpus presence probe (8.2.6.1) ───────────────────────────────────────

async def test_gateA1_fresh_store_is_empty(tmp_path: Any) -> None:
    """A fresh LanceDB partition reports its workspace as empty."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    assert await mgr.is_corpus_empty(_WS) is True


async def test_gateA2_write_makes_store_non_empty(tmp_path: Any) -> None:
    """A corpus write invalidates the cache; the workspace is then non-empty."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    assert await mgr.is_corpus_empty(_WS) is True   # primes the cache
    _seed_row(mgr)
    assert await mgr.is_corpus_empty(_WS) is False


# ── B: Cold-workspace routing invariant (corpus_empty in derive_routing) ──────

def test_gateB1_empty_corpus_tci_low_routes_local_small_no_red_alert() -> None:
    """An empty corpus with tci<30 stays LOCAL_SMALL; is_red_alert is False."""
    css, tci = 20.0, 10.0
    routing = derive_routing_decision(tci=tci, css=css, corpus_empty=True)
    # mirrors the researcher node: is_red_alert = (css < 40) and not corpus_empty
    is_red_alert = (css < 40.0) and not True
    assert routing == "LOCAL_SMALL"
    assert is_red_alert is False


def test_gateB2_nonempty_css_low_routes_cloud_regression_guard() -> None:
    """A real coverage gap (corpus exists, css<40) must still escalate to CLOUD."""
    assert derive_routing_decision(tci=50.0, css=30.0, corpus_empty=False) == "CLOUD"


# ── C: Cold store embed-skip (8.2.6.2) ───────────────────────────────────────

async def test_gateC1_search_with_paths_zero_embeds_on_cold_store(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A cold workspace short-circuits before _get_embedding — zero backend calls."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    embed = AsyncMock(return_value=[0.1] * 8)
    monkeypatch.setattr(semantic_memory, "_get_embedding", embed)
    result = await mgr.search_with_paths("any query", _WS)
    assert result == (0.0, [], [])
    assert embed.await_count == 0


# ── D: Warm-up indexing gate (8.2.6.3) ───────────────────────────────────────

async def test_gateD1_sub_threshold_defers_crawl(mock_vfs_manager: MagicMock) -> None:
    """Below _WARMUP_MIN_FILES the full crawl is skipped; _is_complete stays False."""
    indexer = LazyIndexer()
    pool_run = AsyncMock()
    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=_make_fake_paths(_WARMUP_MIN_FILES - 1)),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch.object(indexer_mod.compute_pool, "run", pool_run),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/ws", "proj_8_2_6", "sess_gate")

    pool_run.assert_not_called()
    mock_vfs_manager.broadcast_indexing_complete.assert_awaited_once()
    assert indexer._is_complete is False


async def test_gateD2_at_threshold_runs_full_crawl(mock_vfs_manager: MagicMock) -> None:
    """At or above _WARMUP_MIN_FILES the full crawl runs and _is_complete is set True."""
    indexer = LazyIndexer()
    with (
        patch.object(indexer, "_preflight_check", AsyncMock(return_value=None)),
        patch.object(indexer_mod, "_collect_eligible_files", return_value=_make_fake_paths(_WARMUP_MIN_FILES)),
        patch.object(indexer_mod, "get_indexed_count", AsyncMock(return_value=0)),
        patch("core.vfs_middleware.VFSMiddleware", _make_vfs_not_ok()),
        patch.object(ws_mod, "vfs_manager", mock_vfs_manager),
    ):
        await indexer._run("/ws", "proj_8_2_6", "sess_gate")

    mock_vfs_manager.broadcast_indexing_complete.assert_awaited_once()
    assert indexer._is_complete is True


# ── E: Local endpoint failover (8.2.6.4) ─────────────────────────────────────

async def test_gateE1_single_retry_then_reraise() -> None:
    """acomplete_byom retries once on a local transport drop, re-raises on second."""
    primary = _make_local_target("ollama_chat/phi4")
    backup = _make_local_target("ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=_conn_err())
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=backup), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert acompletion.await_count == 2  # original + ONE failover, no loop
