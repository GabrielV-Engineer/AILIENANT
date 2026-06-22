# ailienant-core/tests/test_corpus_presence.py
#
# Focused tests for the corpus-presence probe that lets the router tell a cold/empty
# workspace ("nothing to retrieve") apart from a rich-but-low-coverage one.
#
# DoD: pytest tests/test_corpus_presence.py -v must pass with 0 failures.

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from core.memory import semantic_memory
from core.memory.semantic_memory import SemanticMemoryManager

# Synthetic in-dim vector matching the dimension seeded by _seed_row, so a patched
# embedder can drive the real query path on a non-empty store.
_FAKE_VECTOR: List[float] = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

_WS = "ws_corpus_probe_001"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_presence_cache() -> Any:
    """Keep the module-level presence cache isolated per test."""
    semantic_memory._corpus_presence_cache.clear()
    yield
    semantic_memory._corpus_presence_cache.clear()


def _seed_row(mgr: SemanticMemoryManager, file_path: str = "a.py") -> None:
    """Write one row directly, bypassing the litellm embedding backend (hermetic)."""
    record: Dict[str, Any] = {
        "file_path": file_path,
        "workspace_hash": _WS,
        "content_snippet": "def f(): return 1",
        "token_count": 5,
        "vector": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_record(record, workspace_hash=_WS, file_path=file_path, hash_valid=True)


@pytest.mark.anyio
async def test_fresh_store_is_empty(tmp_path) -> None:
    """A store with no table yet reports the workspace as empty."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    assert await mgr.is_corpus_empty(_WS) is True


@pytest.mark.anyio
async def test_store_not_empty_after_write(tmp_path) -> None:
    """A write makes the workspace non-empty, and invalidation re-probes correctly."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    assert await mgr.is_corpus_empty(_WS) is True  # primes the cache as empty
    _seed_row(mgr)                                  # write invalidates the cache
    assert await mgr.is_corpus_empty(_WS) is False


@pytest.mark.anyio
async def test_other_workspace_does_not_count(tmp_path) -> None:
    """Rows for a different workspace must not make this one look non-empty."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    _seed_row(mgr, file_path="a.py")  # seeds under _WS
    assert await mgr.is_corpus_empty("ws_unrelated_999") is True


@pytest.mark.anyio
async def test_blank_or_unsafe_hash_treated_non_empty(tmp_path) -> None:
    """A blank/non-allowlisted hash returns False so the CLOUD floor is never dropped."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    assert await mgr.is_corpus_empty("") is False
    assert await mgr.is_corpus_empty("bad hash!") is False


@pytest.mark.anyio
async def test_cache_hit_avoids_requery(tmp_path, monkeypatch) -> None:
    """A second probe within the TTL serves from cache without re-hitting LanceDB."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))

    calls = {"n": 0}
    real_sync = mgr._is_corpus_empty_sync

    def _counting(ws: str) -> bool:
        calls["n"] += 1
        return real_sync(ws)

    monkeypatch.setattr(mgr, "_is_corpus_empty_sync", _counting)

    assert await mgr.is_corpus_empty(_WS) is True
    assert await mgr.is_corpus_empty(_WS) is True
    assert calls["n"] == 1  # second call hit the cache


# ── 8.2.6.2: embed-skip on a cold store ─────────────────────────────────────────


@pytest.mark.anyio
async def test_search_with_paths_skips_embed_on_cold_store(tmp_path, monkeypatch) -> None:
    """A cold workspace returns the empty shape WITHOUT spending an embedding call."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    embed = AsyncMock(return_value=_FAKE_VECTOR)
    monkeypatch.setattr(semantic_memory, "_get_embedding", embed)

    assert await mgr.search_with_paths("any query", _WS) == (0.0, [], [])
    assert embed.await_count == 0  # DoD: zero embeds on a cold workspace


@pytest.mark.anyio
async def test_search_snippets_skips_embed_on_cold_store(tmp_path, monkeypatch) -> None:
    """The live-chat injection path also skips the embed on a cold workspace."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    embed = AsyncMock(return_value=_FAKE_VECTOR)
    monkeypatch.setattr(semantic_memory, "_get_embedding", embed)

    assert await mgr.search_snippets("any query", _WS) == []
    assert embed.await_count == 0


@pytest.mark.anyio
async def test_non_empty_store_still_embeds(tmp_path, monkeypatch) -> None:
    """Regression guard: a populated corpus must still embed (short-circuit off)."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    _seed_row(mgr)  # write invalidates the presence cache -> is_corpus_empty False
    embed = AsyncMock(return_value=_FAKE_VECTOR)
    monkeypatch.setattr(semantic_memory, "_get_embedding", embed)

    await mgr.search_with_paths("any query", _WS)
    await mgr.search_snippets("any query", _WS)
    assert embed.await_count == 2  # one per method — neither short-circuited
