# ailienant-core/tests/test_graphrag_retrieval_fidelity.py
"""Regression tests for the three GraphRAG retrieval-fidelity fixes.

  - DEBT-141: embed-input truncation is now logged (path, real token count,
    ceiling, tokens dropped) and the ceiling is resolved from the active BYOM
    embedding target instead of a fixed module constant.
  - DEBT-142: search_snippets returns an AST skeleton of the whole matched file
    instead of the stored 500-char head-of-file slice, with layered hang
    containment (an oversized input never reaches the parser; a stalled parse
    degrades to the fallback via a bounded await).
  - DEBT-143: deep_parse ranks expanded neighbors by PPR (seeds keep their own
    vector-relevance order) and caps the read/parse loop by real content
    tokens and file count — never a path-length proxy — while coverage_ratio
    stays computed against the pre-cap target set, so a truncated run cannot
    misreport as complete. extract()/ExtractionResult/_apply_guardrails are
    confirmed removed (they had zero callers in prod and tests).

DoD: pytest tests/test_graphrag_retrieval_fidelity.py -v must pass with 0 failures.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

import core.memory.graphrag_extractor as gre
from core.config.byom_config import EmbeddingTarget
from core.memory import semantic_memory
from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
from core.memory.semantic_memory import SemanticMemoryManager

_WS = "ws_retrieval_fidelity_001"
_FAKE_VECTOR: List[float] = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_presence_cache() -> Any:
    """Keep the module-level corpus-presence cache isolated per test."""
    semantic_memory._corpus_presence_cache.clear()
    yield
    semantic_memory._corpus_presence_cache.clear()


def _mk_target(max_input_tokens: int = 8191) -> EmbeddingTarget:
    return EmbeddingTarget(
        model="test/embed", provider="ollama", dim=8, is_local=True,
        max_input_tokens=max_input_tokens,
    )


def _seed_row(mgr: SemanticMemoryManager, file_path: str, snippet: str) -> None:
    """Write one row directly, bypassing the litellm embedding backend (hermetic)."""
    record: Dict[str, Any] = {
        "file_path": file_path,
        "workspace_hash": _WS,
        "content_snippet": snippet,
        "token_count": 5,
        "vector": list(_FAKE_VECTOR),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    mgr._write_record(record, workspace_hash=_WS, file_path=file_path, hash_valid=True)


# ═════════════════════════════════════════════════════════════════════════
# DEBT-141 — truncation observability + provider-resolved ceiling
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_truncation_logs_warning_with_real_counts(tmp_path, monkeypatch, caplog) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "get_embedding_target", lambda: _mk_target(50))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    content = "def f():\n    return 1\n" * 60  # comfortably over both _MIN_TOKENS and the 50-token ceiling

    with caplog.at_level(logging.WARNING, logger="SEMANTIC_MEMORY"):
        ok = await mgr.semantic_upsert("big.py", content, _WS)

    assert ok is True
    warnings = [r.message for r in caplog.records if "truncating big.py" in r.message]
    assert len(warnings) == 1
    assert "50-token ceiling" in warnings[0]


@pytest.mark.anyio
async def test_ceiling_resolves_from_embedding_target_not_fixed_constant(tmp_path, monkeypatch) -> None:
    """A non-default max_input_tokens on the active target changes where the cut lands."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    captured: Dict[str, int] = {}

    async def _fake_embed(text: str) -> List[float]:
        captured["chars"] = len(text)
        return list(_FAKE_VECTOR)

    monkeypatch.setattr(semantic_memory, "get_embedding_target", lambda: _mk_target(20))
    monkeypatch.setattr(semantic_memory, "_get_embedding", _fake_embed)
    content = "def f():\n    return 1\n" * 200

    await mgr.semantic_upsert("big2.py", content, _WS)

    assert captured["chars"] < len(content)  # truncated well short of the full content


@pytest.mark.anyio
async def test_no_truncation_under_ceiling_is_silent(tmp_path, monkeypatch, caplog) -> None:
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "get_embedding_target", lambda: _mk_target(8191))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    content = "def f():\n    return 1\n" * 30  # over _MIN_TOKENS, nowhere near 8191

    with caplog.at_level(logging.WARNING, logger="SEMANTIC_MEMORY"):
        await mgr.semantic_upsert("small.py", content, _WS)

    assert not any("truncating" in r.message for r in caplog.records)


# ═════════════════════════════════════════════════════════════════════════
# DEBT-142 — evidence granularity: query-time skeleton distillation
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_search_snippets_returns_skeleton_not_head_slice(tmp_path, monkeypatch) -> None:
    """A match past the file's first 500 chars must not be reduced to its import header."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))

    project_root = tmp_path / "proj"
    project_root.mkdir()
    target = project_root / "big_module.py"
    padding = "\n".join(f"CONST_{i} = {i}" for i in range(120))
    target.write_text(
        f"import os\nimport sys\n\n{padding}\n\n"
        "def needle_function(x):\n"
        '    """The function that actually matched the query."""\n'
        "    return x + 1\n",
        encoding="utf-8",
    )
    assert len(target.read_text(encoding="utf-8")) > 500  # sanity: exceeds the old [:500] slice

    _seed_row(mgr, "big_module.py", "import os\nimport sys")  # what a head-slice would have looked like

    results = await mgr.search_snippets(
        "needle", workspace_hash=_WS, project_root=str(project_root)
    )

    assert len(results) == 1
    path, evidence = results[0]
    assert path == "big_module.py"
    assert "needle_function" in evidence
    assert "actually matched the query" in evidence


@pytest.mark.anyio
async def test_search_snippets_falls_back_when_no_project_root(tmp_path, monkeypatch) -> None:
    """Omitting project_root degrades gracefully to the stored content_snippet."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    _seed_row(mgr, "missing.py", "the stored fallback slice")

    results = await mgr.search_snippets("q", workspace_hash=_WS)

    assert results == [("missing.py", "the stored fallback slice")]


@pytest.mark.anyio
async def test_search_snippets_falls_back_on_oversized_content(tmp_path, monkeypatch) -> None:
    """Content past the distill size guard never reaches the parser (Layer 1 containment)."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    project_root = tmp_path / "proj2"
    project_root.mkdir()
    (project_root / "huge.py").write_text("x = 1\n" * 100_000, encoding="utf-8")
    _seed_row(mgr, "huge.py", "fallback slice")

    calls = {"n": 0}
    real_extract = __import__("core.ast_engine", fromlist=["extract_skeleton"]).extract_skeleton

    def _counting_extract(content: str, lang: str) -> str:
        calls["n"] += 1
        return real_extract(content, lang)

    monkeypatch.setattr("core.ast_engine.extract_skeleton", _counting_extract)

    results = await mgr.search_snippets("q", workspace_hash=_WS, project_root=str(project_root))

    assert results == [("huge.py", "fallback slice")]
    assert calls["n"] == 0  # the size guard skipped the parser entirely


@pytest.mark.anyio
async def test_search_snippets_falls_back_on_distill_timeout(tmp_path, monkeypatch) -> None:
    """A stalled parse degrades to the fallback via the bounded await, never hangs the caller."""
    mgr = SemanticMemoryManager(lancedb_path=str(tmp_path / "lance"))
    monkeypatch.setattr(semantic_memory, "_get_embedding", AsyncMock(return_value=list(_FAKE_VECTOR)))
    monkeypatch.setattr(mgr, "_DISTILL_TIMEOUT_S", 0.05)
    project_root = tmp_path / "proj3"
    project_root.mkdir()
    (project_root / "slow.py").write_text("def f(): return 1\n", encoding="utf-8")
    _seed_row(mgr, "slow.py", "fallback slice")

    def _slow_extract(content: str, lang: str) -> str:
        time.sleep(0.3)  # simulates a stalled tree-sitter parse
        return "def f(): ..."

    monkeypatch.setattr("core.ast_engine.extract_skeleton", _slow_extract)

    results = await mgr.search_snippets("q", workspace_hash=_WS, project_root=str(project_root))

    assert results == [("slow.py", "fallback slice")]


@pytest.mark.anyio
async def test_fetch_rag_snippets_forwards_workspace_root_as_project_root() -> None:
    """agents/coder.py must thread workspace_root through so evidence can be distilled."""
    from agents.coder import _fetch_rag_snippets

    captured: Dict[str, Any] = {}

    async def _fake_search(
        query: str, workspace_hash: str = "", k: int = 3, project_root: Optional[str] = None,
    ) -> List[Any]:
        captured["project_root"] = project_root
        return []

    await _fetch_rag_snippets(
        "target.py", "desc", "proj-1", _fake_search, workspace_root="/ws/root",
    )

    assert captured["project_root"] == "/ws/root"


@pytest.mark.anyio
async def test_query_memory_mcp_handler_shape_unchanged(monkeypatch) -> None:
    """The external MCP contract (list of {file_path, snippet} dicts) must not change."""
    from gateway.handlers import handle_query_memory

    async def _fake_search_snippets(
        self: SemanticMemoryManager,
        query: str,
        workspace_hash: str = "",
        k: int = 5,
        project_root: Optional[str] = None,
    ) -> List[Any]:
        assert project_root == "/some/workspace"  # DEBT-142 wiring reached the MCP handler too
        return [("f.py", "def g(): ...")]

    monkeypatch.setattr(SemanticMemoryManager, "search_snippets", _fake_search_snippets)

    out = await handle_query_memory({"query": "q", "workspace_root": "/some/workspace"})

    assert out == [{"file_path": "f.py", "snippet": "def g(): ..."}]


# ═════════════════════════════════════════════════════════════════════════
# DEBT-143 — deep_parse: PPR-ranked neighbors, real-token budget cap
# ═════════════════════════════════════════════════════════════════════════


def _mk_extractor(
    monkeypatch: pytest.MonkeyPatch,
    neighbors: List[str],
    ppr: Optional[Dict[str, float]] = None,
) -> GraphRAGDynamicExtractor:
    """A GraphRAGDynamicExtractor with the SQLite-backed methods stubbed out —
    deep_parse's own logic (ranking, capping, coverage) is what's under test,
    not aiosqlite plumbing already covered by the BFS/PPR tests it delegates to.
    """
    extractor = GraphRAGDynamicExtractor(project_id="proj-x")

    async def _fake_expand(seed_files: List[str]) -> List[str]:
        return list(neighbors)

    async def _fake_ppr(files: List[str]) -> Dict[str, float]:
        return {f: (ppr or {}).get(f, 0.0) for f in files}

    monkeypatch.setattr(extractor, "_expand_neighbors", _fake_expand)
    monkeypatch.setattr(extractor, "_fetch_ppr_scores", _fake_ppr)
    return extractor


def _write_py(root: Any, name: str, body: str = "def f():\n    return 1\n") -> None:
    (root / name).write_text(body, encoding="utf-8")


@pytest.mark.anyio
async def test_deep_parse_seeds_first_neighbors_ppr_ranked(tmp_path, monkeypatch) -> None:
    """Seeds keep the caller's vector-relevance order; PPR only ranks neighbors."""
    root = tmp_path / "proj"
    root.mkdir()
    for name in ("s1.py", "s2.py", "n1.py", "n2.py", "n3.py"):
        _write_py(root, name)
    extractor = _mk_extractor(
        monkeypatch,
        neighbors=["n1.py", "n2.py", "n3.py"],
        ppr={"n1.py": 0.5, "n2.py": 0.1, "n3.py": 0.9},
    )

    result = await extractor.deep_parse(["s1.py", "s2.py"], str(root))

    assert result.target_files == ["s1.py", "s2.py", "n3.py", "n1.py", "n2.py"]


@pytest.mark.anyio
async def test_deep_parse_caps_file_count_without_inflating_coverage(tmp_path, monkeypatch) -> None:
    """Error-1 regression guard: capping must shrink coverage_ratio, never raise it."""
    root = tmp_path / "proj"
    root.mkdir()
    seeds = [f"s{i}.py" for i in range(3)]
    neighbors = [f"n{i}.py" for i in range(12)]
    for name in seeds + neighbors:
        _write_py(root, name)
    monkeypatch.setitem(gre._MAX_FILES, "LOCAL_SMALL", 5)
    monkeypatch.setitem(gre._TOKEN_CEILING, "LOCAL_SMALL", 100_000)  # generous — file count binds
    extractor = _mk_extractor(monkeypatch, neighbors=neighbors)

    result = await extractor.deep_parse(seeds, str(root))

    assert len(result.parsed_files) == 5
    assert len(result.target_files) == 15  # PRE-cap — never shrunk by the guardrail
    assert result.truncated is True
    assert result.coverage_ratio == pytest.approx(5 / 15)
    assert result.coverage_ratio < 1.0


@pytest.mark.anyio
async def test_deep_parse_token_ceiling_stops_early(tmp_path, monkeypatch) -> None:
    """The token ceiling can bind before the file-count cap does."""
    root = tmp_path / "proj"
    root.mkdir()
    seeds = ["s0.py"]
    neighbors = [f"n{i}.py" for i in range(5)]
    for name in seeds + neighbors:
        _write_py(root, name, body="def f():\n    return 1\n" * 5)
    monkeypatch.setitem(gre._MAX_FILES, "LOCAL_SMALL", 100)      # generous — token ceiling binds
    monkeypatch.setitem(gre._TOKEN_CEILING, "LOCAL_SMALL", 30)   # small enough to bite quickly
    extractor = _mk_extractor(monkeypatch, neighbors=neighbors)

    result = await extractor.deep_parse(seeds, str(root))

    assert result.truncated is True
    assert len(result.parsed_files) < len(result.target_files)
    assert result.token_count <= 30


@pytest.mark.anyio
async def test_deep_parse_seed_only_when_no_neighbors(tmp_path, monkeypatch) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _write_py(root, "solo.py")
    extractor = _mk_extractor(monkeypatch, neighbors=[])

    result = await extractor.deep_parse(["solo.py"], str(root))

    assert result.target_files == ["solo.py"]
    assert result.parsed_files == ["solo.py"]
    assert result.truncated is False
    assert result.coverage_ratio == 1.0


@pytest.mark.anyio
async def test_deep_parse_empty_seeds_returns_empty_result() -> None:
    extractor = GraphRAGDynamicExtractor(project_id="proj-x")

    result = await extractor.deep_parse([], "/nonexistent")

    assert result.target_files == []
    assert result.truncated is False


def test_extract_and_dead_guardrails_removed() -> None:
    """Locks in the DEBT-143 deletion: zero callers in prod and tests before removal."""
    assert not hasattr(GraphRAGDynamicExtractor, "extract")
    assert not hasattr(GraphRAGDynamicExtractor, "_apply_guardrails")
    assert not hasattr(gre, "ExtractionResult")
