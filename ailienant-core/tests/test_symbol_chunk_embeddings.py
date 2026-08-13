# ailienant-core/tests/test_symbol_chunk_embeddings.py
"""Regression tests for symbol-level chunk embeddings (DEBT-140).

GraphRAG embeds one vector per whole file, which caps retrieval precision on
any multi-function file. This suite covers the hybrid-by-size chunk table
(`symbol_chunk_embeddings`) that supplements — never replaces — the existing
file-level `workspace_embeddings` table:

  - Hybrid gate: only files >= _CHUNK_FILE_MIN_TOKENS emit chunk rows; classes
    and trivial (< _CHUNK_MIN_TOKENS) chunks are excluded.
  - Content-addressed reuse: a chunk's vector is keyed on a hash of its own
    text, not its position, so incremental cost scales with symbols actually
    edited, not with file size or line-shift noise above the symbol.
  - Batched, partitioned embedding calls (item count AND token budget), with a
    sequential fallback when a provider under-returns vectors.
  - The read path (search_snippets) merges file + chunk evidence under a
    concurrent `asyncio.gather`, packs multi-hit evidence under a bounded
    per-file budget, and degrades cleanly when the chunk table/query fails.
  - Rot prevention: delete, vector GC, and dimension-mismatch recreation all
    cover both tables.
  - The routing meters (search / search_with_paths) are provably unaffected by
    chunk rows.
  - Bounded, resumable backfill for a corpus indexed before chunking existed.

DoD: pytest tests/test_symbol_chunk_embeddings.py -v must pass with 0 failures.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence
from unittest.mock import AsyncMock

import pytest

from core.memory import semantic_memory
from core.memory.semantic_memory import SemanticMemoryManager
from shared.contracts import IndexingResult, SymbolDef

_WS = "ws_chunk_embed_001"
_FAKE_VECTOR: List[float] = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_presence_cache() -> Any:
    semantic_memory._corpus_presence_cache.clear()
    yield
    semantic_memory._corpus_presence_cache.clear()


@pytest.fixture(autouse=True)
def _small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the size gates so tiny in-test content can exercise both tiers.

    The module-level ordering invariant (_CHUNK_FILE_MIN_TOKENS > _MIN_TOKENS,
    100) is preserved so these values stay representative of the real gate.
    """
    monkeypatch.setattr(semantic_memory, "_CHUNK_FILE_MIN_TOKENS", 120)
    monkeypatch.setattr(semantic_memory, "_CHUNK_MIN_TOKENS", 5)


def _mk_embedder(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Stub _get_embeddings; records each call's texts for assertion."""
    calls: List[List[str]] = []

    async def _fake(texts: Sequence[str]) -> List[List[float]]:
        calls.append(list(texts))
        return [list(_FAKE_VECTOR) for _ in texts]

    monkeypatch.setattr(semantic_memory, "_get_embeddings", _fake)
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    return calls


def _sym(name: str, kind: str, start: int, end: int) -> SymbolDef:
    return SymbolDef(qualified_name=name, kind=kind, start_line=start, end_line=end)


def _big_body(n_lines: int = 40) -> str:
    """~content well over the (shrunk) 120-token file gate."""
    return "\n".join(f"    x{i} = {i}" for i in range(n_lines))


def _seed_file_row(
    mgr: SemanticMemoryManager, file_path: str, snippet: str, token_count: int = 5,
) -> None:
    record: Dict[str, Any] = {
        "file_path": file_path,
        "workspace_hash": _WS,
        "content_snippet": snippet,
        "token_count": token_count,
        "vector": list(_FAKE_VECTOR),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_record(record, workspace_hash=_WS, file_path=file_path, hash_valid=True)


def _seed_chunk_row(
    mgr: SemanticMemoryManager,
    file_path: str,
    qualified_name: str,
    chunk_text: str,
    vector: List[float] | None = None,
) -> None:
    record: Dict[str, Any] = {
        "file_path": file_path,
        "workspace_hash": _WS,
        "qualified_name": qualified_name,
        "kind": "function",
        "start_line": 1,
        "end_line": 2,
        "chunk_text": chunk_text,
        "content_hash": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        "token_count": 5,
        "vector": list(vector or _FAKE_VECTOR),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_chunk_records([record], file_path, _WS, build_index=False)


# ═════════════════════════════════════════════════════════════════════════
# Hybrid gate + chunk construction
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_over_threshold_file_writes_chunk_rows(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    _mk_embedder(monkeypatch)
    content = f"def big_fn():\n{_big_body()}\n    return 1\n"
    symbols = [_sym("big_fn", "function", 1, content.count(chr(10)) + 1)]

    ok = await mgr.semantic_upsert("big.py", content, _WS, symbols=symbols)

    assert ok is True
    vectors = mgr._existing_chunk_vectors("big.py", _WS)
    assert len(vectors) == 1


@pytest.mark.anyio
async def test_under_threshold_file_writes_no_chunks(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    _mk_embedder(monkeypatch)
    content = "def f():\n    return 1\n" * 10  # over _MIN_TOKENS, under the 120-token chunk gate
    symbols = [_sym("f", "function", 1, 2)]

    ok = await mgr.semantic_upsert("small.py", content, _WS, symbols=symbols)

    assert ok is True
    assert mgr._existing_chunk_vectors("small.py", _WS) == {}


@pytest.mark.anyio
async def test_chunk_min_tokens_drops_trivial_keeps_real(tmp_path, monkeypatch) -> None:
    """The spec's flagged failure mode: a floor too high silently writes nothing."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    calls = _mk_embedder(monkeypatch)
    content = (
        "def getter():\n    return 1\n\n"
        f"def real_fn():\n{_big_body()}\n    return 2\n"
    )
    lines = content.splitlines()
    getter_end = next(i for i, ln in enumerate(lines, 1) if "return 1" in ln)
    real_start = next(i for i, ln in enumerate(lines, 1) if "def real_fn" in ln)
    getter_tokens = len(semantic_memory._ENC.encode("\n".join(lines[0:getter_end])))
    # A floor strictly between the two chunks' token counts is what actually
    # exercises the gate — too low (the fixture default) admits both.
    monkeypatch.setattr(semantic_memory, "_CHUNK_MIN_TOKENS", getter_tokens + 1)
    symbols = [
        _sym("getter", "function", 1, getter_end),
        _sym("real_fn", "function", real_start, len(lines)),
    ]

    await mgr.semantic_upsert("mixed.py", content, _WS, symbols=symbols)

    embedded_texts = calls[0]
    assert not any("return 1" in t and "getter" not in t for t in embedded_texts)
    assert any("real_fn" in t for t in embedded_texts)
    assert len(embedded_texts) == 1  # only real_fn cleared the floor


@pytest.mark.anyio
async def test_class_rows_excluded_and_empty_slice_skipped(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    calls = _mk_embedder(monkeypatch)
    content = f"class Big:\n{_big_body()}\n    def method(self):\n        return 1\n"
    symbols = [
        _sym("Big", "class", 1, content.count(chr(10)) + 1),
        _sym("Big.method", "method", content.count(chr(10)), content.count(chr(10)) + 1),
        _sym("bogus", "function", 999, 5),  # malformed range → empty slice, must be skipped
    ]

    await mgr.semantic_upsert("cls.py", content, _WS, symbols=symbols)

    for texts in calls:
        for t in texts:
            assert "class Big" not in t  # the class row itself never reaches the embedder
    assert all(t.strip() for texts in calls for t in texts)  # nothing empty was sent


# ═════════════════════════════════════════════════════════════════════════
# Failure isolation
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_chunk_write_failure_leaves_file_row_intact(tmp_path, monkeypatch, caplog) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))

    async def _boom(texts: Sequence[str]) -> List[List[float]]:
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(semantic_memory, "_get_embeddings", _boom)
    content = f"def big_fn():\n{_big_body()}\n    return 1\n"
    symbols = [_sym("big_fn", "function", 1, content.count(chr(10)) + 1)]

    with caplog.at_level(logging.WARNING, logger="SEMANTIC_MEMORY"):
        ok = await mgr.semantic_upsert("big.py", content, _WS, symbols=symbols)

    assert ok is True  # circuit breaker must see success — the file-level write stood
    assert mgr._existing_chunk_vectors("big.py", _WS) == {}
    assert any("chunk write failed" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_partial_batch_failure_writes_zero_chunk_rows(tmp_path, monkeypatch) -> None:
    """All-or-nothing: one bad embed in the batch must not publish a partial set."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    monkeypatch.setattr(
        semantic_memory, "_get_embeddings", AsyncMock(side_effect=RuntimeError("boom"))
    )
    content = (
        f"def fn_a():\n{_big_body(15)}\n    return 1\n\n"
        f"def fn_b():\n{_big_body(15)}\n    return 2\n"
    )
    lines = content.splitlines()
    mid = next(i for i, ln in enumerate(lines, 1) if "def fn_b" in ln)
    symbols = [_sym("fn_a", "function", 1, mid - 1), _sym("fn_b", "function", mid, len(lines))]

    ok = await mgr.semantic_upsert("two_fns.py", content, _WS, symbols=symbols)

    assert ok is True
    assert mgr._existing_chunk_vectors("two_fns.py", _WS) == {}


# ═════════════════════════════════════════════════════════════════════════
# Batched embedding (R1)
# ═════════════════════════════════════════════════════════════════════════


def test_partition_closes_on_item_count() -> None:
    texts = [f"t{i}" for i in range(70)]  # short texts — count binds before token budget
    batches = semantic_memory._partition_for_embedding(texts, token_budget=100_000)
    assert [len(b) for b in batches] == [32, 32, 6]


def test_partition_closes_on_token_budget() -> None:
    texts = ["word " * 50 for _ in range(5)]  # ~50 tokens each
    batches = semantic_memory._partition_for_embedding(texts, token_budget=120)
    assert all(
        sum(len(semantic_memory._ENC.encode(texts[i])) for i in b) <= 120 or len(b) == 1
        for b in batches
    )
    assert sum(len(b) for b in batches) == 5


@pytest.mark.anyio
async def test_get_embeddings_preserves_order_across_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    texts = [f"text-{i}" for i in range(40)]  # spans two batches at size 32

    async def _fake_batch(batch_texts: List[str]) -> List[List[float]]:
        return [[float(len(t))] for t in batch_texts]

    monkeypatch.setattr(semantic_memory, "_embed_batch", _fake_batch)

    vectors = await semantic_memory._get_embeddings(texts)

    assert vectors == [[float(len(t))] for t in texts]


@pytest.mark.anyio
async def test_embed_batch_falls_back_when_provider_under_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        data = [{"embedding": [9.9], "index": 0}]  # only 1 vector for a 3-text batch

    async def _fake_aembedding(**kwargs: Any) -> Any:
        return _Resp()

    calls = {"single": 0}

    async def _fake_single(text: str) -> List[float]:
        calls["single"] += 1
        return [1.0]

    import litellm  # semantic_memory._embed_batch imports litellm locally (deferred)

    monkeypatch.setattr(semantic_memory, "get_embedding_target", lambda: _mk_target())
    monkeypatch.setattr(litellm, "aembedding", _fake_aembedding)
    monkeypatch.setattr(semantic_memory, "_get_embedding", _fake_single)

    result = await semantic_memory._embed_batch(["a", "b", "c"])

    assert result == [[1.0], [1.0], [1.0]]
    assert calls["single"] == 3


def _mk_target() -> Any:
    from core.config.byom_config import EmbeddingTarget
    return EmbeddingTarget(model="test/embed", provider="ollama", dim=8, is_local=True)


# ═════════════════════════════════════════════════════════════════════════
# Content-addressed reuse (finding 11) — the incremental-cost guard
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_reuse_unchanged_file_embeds_nothing(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    calls = _mk_embedder(monkeypatch)
    content = f"def fn_a():\n{_big_body()}\n    return 1\n"
    symbols = [_sym("fn_a", "function", 1, content.count(chr(10)) + 1)]

    await mgr.semantic_upsert("stable.py", content, _WS, symbols=symbols)
    calls.clear()
    await mgr.semantic_upsert("stable.py", content, _WS, symbols=symbols)

    assert calls == []  # a byte-identical re-upsert embeds zero chunks


@pytest.mark.anyio
async def test_reuse_one_edited_function_embeds_exactly_one(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    calls = _mk_embedder(monkeypatch)
    body_a, body_b = _big_body(20), _big_body(21)
    content_v1 = f"def fn_a():\n{body_a}\n    return 1\n\ndef fn_b():\n{body_b}\n    return 2\n"
    lines = content_v1.splitlines()
    mid = next(i for i, ln in enumerate(lines, 1) if "def fn_b" in ln)
    symbols = [_sym("fn_a", "function", 1, mid - 1), _sym("fn_b", "function", mid, len(lines))]
    await mgr.semantic_upsert("two.py", content_v1, _WS, symbols=symbols)
    calls.clear()

    content_v2 = content_v1.replace("return 1", "return 999")  # only fn_a's body changed
    await mgr.semantic_upsert("two.py", content_v2, _WS, symbols=symbols)

    assert len(calls) == 1 and len(calls[0]) == 1
    assert "999" in calls[0][0]


@pytest.mark.anyio
async def test_reuse_survives_a_line_shift_above_the_function(tmp_path, monkeypatch) -> None:
    """Proves the reuse key is the text hash, not (qualified_name, start_line)."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    calls = _mk_embedder(monkeypatch)
    content_v1 = f"def fn_a():\n{_big_body()}\n    return 1\n"
    symbols_v1 = [_sym("fn_a", "function", 1, content_v1.count(chr(10)) + 1)]
    await mgr.semantic_upsert("shift.py", content_v1, _WS, symbols=symbols_v1)
    calls.clear()

    # Insert an unrelated line above fn_a — every subsequent line number shifts.
    content_v2 = "COMMENT = 1\n" + content_v1
    symbols_v2 = [_sym("fn_a", "function", s + 1, e + 1) for s, e in [(1, content_v1.count(chr(10)) + 1)]]
    await mgr.semantic_upsert("shift.py", content_v2, _WS, symbols=symbols_v2)

    assert calls == []  # same text, shifted position — reused, not re-embedded
    vectors = mgr._existing_chunk_vectors("shift.py", _WS)
    assert len(vectors) == 1


# ═════════════════════════════════════════════════════════════════════════
# Read path: gather (R2), knapsack (R3), merge, additive tolerance
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_search_snippets_degrades_when_chunk_query_raises(tmp_path, monkeypatch, caplog) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "f.py", "fallback slice")

    def _boom(vector: List[float], workspace_hash: str, k: int) -> Any:
        raise RuntimeError("lance backend hiccup")

    monkeypatch.setattr(mgr, "_query_chunks", _boom)

    with caplog.at_level(logging.WARNING, logger="SEMANTIC_MEMORY"):
        results = await mgr.search_snippets("q", workspace_hash=_WS)

    assert results == [("f.py", "fallback slice")]
    assert any("chunk query failed" in r.message for r in caplog.records)


def test_evidence_knapsack_bounds_per_file_nearest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic_memory, "_MAX_EVIDENCE_CHARS_PER_FILE", 100)
    hits = [
        (0.1, "A" * 60),
        (0.05, "B" * 60),   # nearest — must be admitted first
        (0.9, "C" * 60),    # farthest — must be dropped
    ]

    packed = SemanticMemoryManager._pack_chunk_evidence(hits)

    assert packed.startswith("B" * 60)
    assert "C" * 60 not in packed
    assert len(packed) <= 100 + 2  # +2 for the "\n\n" join, no chunk is mid-truncated mid-hit


@pytest.mark.anyio
async def test_search_snippets_merges_and_prefers_chunk_evidence(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "chunked.py", "stale head-of-file slice")
    _seed_file_row(mgr, "unchunked.py", "genuine fallback slice")
    _seed_chunk_row(mgr, "chunked.py", "the_real_function", "def the_real_function(): ...")

    results = await mgr.search_snippets("q", workspace_hash=_WS, k=5)

    by_path = dict(results)
    assert by_path["chunked.py"] == "def the_real_function(): ..."
    assert by_path["unchunked.py"] == "genuine fallback slice"


@pytest.mark.anyio
async def test_search_snippets_works_with_no_chunk_table(tmp_path, monkeypatch) -> None:
    """Additive tolerance: a corpus with no chunk table at all must not error."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "solo.py", "the only evidence available")

    results = await mgr.search_snippets("q", workspace_hash=_WS)

    assert results == [("solo.py", "the only evidence available")]


# ═════════════════════════════════════════════════════════════════════════
# Routing isolation (correction 4)
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_search_and_search_with_paths_unaffected_by_chunk_rows(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "routed.py", "snippet")

    baseline_score = await mgr.search("q", workspace_hash=_WS)
    baseline_paths = await mgr.search_with_paths("q", workspace_hash=_WS)

    _seed_chunk_row(mgr, "routed.py", "sym", "def sym(): ...", vector=[9.0] * 8)  # far vector

    after_score = await mgr.search("q", workspace_hash=_WS)
    after_paths = await mgr.search_with_paths("q", workspace_hash=_WS)

    assert after_score == baseline_score
    assert after_paths == baseline_paths


# ═════════════════════════════════════════════════════════════════════════
# Rot prevention
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_semantic_delete_purges_both_tables(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "gone.py", "snippet")
    _seed_chunk_row(mgr, "gone.py", "sym", "def sym(): ...")

    await mgr.semantic_delete("gone.py", workspace_hash=_WS)

    assert mgr._existing_chunk_vectors("gone.py", _WS) == {}
    results = await mgr.search_snippets("q", workspace_hash=_WS)
    assert results == []


def test_vector_gc_reports_orphans_from_both_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked at the lancedb-connection level, mirroring tests/test_janitor.py's own
    convention (that suite never exercises a real to_lance() pushdown, since the
    optional ``pylance`` extra is not assumed present in every environment).
    """
    import pyarrow as pa
    from unittest.mock import MagicMock, patch
    from core.janitor import _vector_gc_sync

    ws = "/my/workspace"
    from core.storage_paths import project_id_for
    ws_hash = project_id_for(ws)
    file_only_orphan = "/my/workspace/file_only.py"   # stale row in the file table only
    chunk_only_orphan = "/my/workspace/chunk_only.py"  # stale row in the chunk table only

    def _arrow_for(path: str) -> pa.Table:
        return pa.table({
            "file_path": pa.array([path], type=pa.utf8()),
            "workspace_hash": pa.array([ws_hash], type=pa.utf8()),
        })

    def _mk_table(path: str) -> MagicMock:
        ds = MagicMock()
        ds.to_table.return_value = _arrow_for(path)
        tbl = MagicMock()
        tbl.to_lance.return_value = ds
        return tbl

    file_tbl = _mk_table(file_only_orphan)
    chunk_tbl = _mk_table(chunk_only_orphan)
    mock_db = MagicMock()
    mock_db.table_names.return_value = ["workspace_embeddings", "symbol_chunk_embeddings"]
    mock_db.open_table.side_effect = lambda name: (
        file_tbl if name == "workspace_embeddings" else chunk_tbl
    )

    with patch("lancedb.connect", return_value=mock_db), \
         patch("core.janitor.os.path.exists", return_value=False):
        report = _vector_gc_sync(ws, "/fake/lancedb")

    assert set(report.orphaned_paths) == {file_only_orphan, chunk_only_orphan}
    assert report.deleted_count == 2
    # Each orphaned path is purged from EVERY table, not just the one it was
    # discovered in — a file orphaned only in the chunk table must still be
    # deleted from the file table (and vice versa) since a no-op delete on an
    # absent row is harmless, while skipping a table risks leaving ghost rows,
    # exactly the rot this GC exists to prevent. Two orphaned paths x two
    # tables = two delete calls per table.
    assert file_tbl.delete.call_count == 2
    assert chunk_tbl.delete.call_count == 2


@pytest.mark.anyio
async def test_dimension_change_drops_both_tables(tmp_path, monkeypatch) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_file_row(mgr, "dimshift.py", "snippet")
    _seed_chunk_row(mgr, "dimshift.py", "sym", "def sym(): ...")

    bigger_record: Dict[str, Any] = {
        "file_path": "dimshift.py", "workspace_hash": _WS, "content_snippet": "s",
        "token_count": 5, "vector": [0.1] * 16,  # different dimension than the seeded 8
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_record(bigger_record, workspace_hash=_WS, file_path="dimshift.py", hash_valid=True)

    assert mgr._existing_chunk_vectors("dimshift.py", _WS) == {}  # chunk table recreated empty


# ═════════════════════════════════════════════════════════════════════════
# Backfill (adoption path)
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_backfill_chunks_idempotent_and_reports_remaining(tmp_path, monkeypatch) -> None:
    import core.compute_pool as cp_mod

    lance_path = str(tmp_path / "lance")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    mgr = SemanticMemoryManager(lancedb_path=lance_path)
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _mk_embedder(monkeypatch)

    for i in range(3):
        name = f"big{i}.py"
        (project_root / name).write_text(f"def fn():\n{_big_body()}\n    return 1\n", encoding="utf-8")
        _seed_file_row(mgr, name, "old snippet", token_count=200)  # over the 120-token gate

    symbols = [_sym("fn", "function", 1, 42)]
    fake_result = IndexingResult(
        file_path="x", symbol_count=1, language_id="python", success=True, symbols=symbols,
    )
    monkeypatch.setattr(cp_mod.compute_pool, "run", AsyncMock(return_value=fake_result))

    first = await mgr.backfill_chunks(_WS, str(project_root), limit=2)
    assert first["processed"] == 2
    assert first["chunked"] == 2
    assert first["remaining"] == 1

    second = await mgr.backfill_chunks(_WS, str(project_root), limit=10)
    assert second["processed"] == 1  # only the one file skipped by the limit last time
    assert second["chunked"] == 1
    assert second["remaining"] == 0

    third = await mgr.backfill_chunks(_WS, str(project_root), limit=10)
    assert third["processed"] == 0  # nothing left un-chunked
    assert third["chunked"] == 0


@pytest.mark.anyio
async def test_backfill_chunks_builds_index_once_not_per_file(tmp_path, monkeypatch) -> None:
    import core.compute_pool as cp_mod

    lance_path = str(tmp_path / "lance")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    mgr = SemanticMemoryManager(lancedb_path=lance_path)
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _mk_embedder(monkeypatch)

    for i in range(3):
        name = f"big{i}.py"
        (project_root / name).write_text(f"def fn():\n{_big_body()}\n    return 1\n", encoding="utf-8")
        _seed_file_row(mgr, name, "old snippet", token_count=200)

    fake_result = IndexingResult(
        file_path="x", symbol_count=1, language_id="python", success=True,
        symbols=[_sym("fn", "function", 1, 42)],
    )
    monkeypatch.setattr(cp_mod.compute_pool, "run", AsyncMock(return_value=fake_result))
    index_calls = {"n": 0}
    real_build = SemanticMemoryManager._build_chunk_index

    def _counting_build(tbl: Any) -> None:
        index_calls["n"] += 1
        real_build(tbl)

    monkeypatch.setattr(SemanticMemoryManager, "_build_chunk_index", staticmethod(_counting_build))

    await mgr.backfill_chunks(_WS, str(project_root), limit=10)

    assert index_calls["n"] == 1  # not once per file
