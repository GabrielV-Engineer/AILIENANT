# tests/test_phase8_14_checkpoint_gate.py
"""Graph Intelligence Upgrade — Division Checkpoint Gate.

Test-only certification that Division 8.14's cross-cutting invariants hold against
their shipped entry points. It imports and invokes production code (``brain.memory``,
``core.blast_radius``, ``core.memory_snapshot``, ``core.dead_code``,
``core.db``), asserting one load-bearing invariant per row; it modifies no production
logic and follows the sibling-gate convention. Each sub-phase already has unit tests
covering its pieces in isolation (``test_polyglot_imports.py``, ``test_blast_radius.py``,
``test_memory_snapshot.py``, ``test_dead_code.py``, ``test_architecture_digest.py``);
this gate re-certifies the guarantees that must hold *together* from the division's
vantage point.

Rows certified here:
  POLY1       Python import extraction is absolute-only; relative forms skipped
  POLY2       a TS relative specifier reaches EXTRACTED (incl. a dir/index.ts
              barrel); a bare specifier stays INFERRED
  POLY3       a workspace-escaping specifier (parent + sibling-prefix) is dropped
  POLY4       extraction + confidence resolution touch no filesystem
  POLY5       an unregistered language yields no edges and never raises
  BLAST1      the reverse-adjacency BFS is cycle-safe
  BLAST2      a 5K-node / 15K-edge radius completes < 500 ms and runs off the
              event loop (asyncio.to_thread offload)
  SNAP1       a snapshot round-trips the enriched edge + PPR + community graph
  SNAP2       an export racing a source rewrite captures a whole set, never torn
  DEAD1       the dead-code allowlist honors the hardcoded set AND the JSON globs
  DIGEST1     the architecture digest is bounded/paginated with a truthful total;
              empty input yields the well-formed skeleton
  DIGEST2     the digest is token-capped (bounded passes) and deterministic
  NOPOLLUTE1  the Tier-2 substrates (symbol_definitions / boundary_edges /
              observed_call_edges) never contaminate the file-level graph reads
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from brain.memory import (
    _MODULE_LIMIT,
    IMPORT_EXTRACTORS,
    _empty_digest,
    _extract_ecmascript_imports,
    _resolve_edge_confidence,
    build_architecture_digest_sync,
    index_file_sync,
)
from core import db as catalog_db
from core import memory_snapshot as ms
from core.ast_engine import ASTEngine
from core.blast_radius import (
    MAX_BLAST_EDGES,
    compute_blast_radius,
    compute_blast_radius_sync,
)
from core.dead_code import compute_dead_code_sync
from shared.contracts import IndexingRequest, SymbolDef
from tools.perception_tools import (
    _DIGEST_TOKEN_CAP,
    _DIGEST_TRIM_ORDER,
    _enforce_digest_token_cap,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _isolate_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "catalog.sqlite"
) -> str:
    db = str(tmp_path / name)
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def _n(p: str) -> str:
    return p.replace("\\", "/")


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str, workspace_root: str, language_id: str = "typescript") -> IndexingRequest:
    return IndexingRequest(
        file_path=path, content="", language_id=language_id, workspace_root=workspace_root
    )


def _reader(mapping: dict[str, str]) -> Callable[[str], Optional[str]]:
    return lambda path: mapping.get(path)


# ── POLY1 ─────────────────────────────────────────────────────────────────────


def test_poly1_python_extraction_absolute_only() -> None:
    src = "import os\nfrom . import x\nfrom .mod import y\nimport a.b.c\nfrom pkg.sub import z\n"
    tree = _parse(src, "python", "/ws/pkg/m.py")
    req = IndexingRequest(
        file_path="/ws/pkg/m.py", content=src, language_id="python", workspace_root="/ws"
    )
    imports = IMPORT_EXTRACTORS["python"](tree, req)
    assert imports == ["os", "a.b.c", "pkg.sub"], "relative imports must be skipped"


# ── POLY2 ─────────────────────────────────────────────────────────────────────


def test_poly2_ts_relative_reaches_extracted_bare_stays_inferred() -> None:
    edges = (
        ("/ws/src/main.ts", "/ws/src/a"),           # resolves via extension candidate
        ("/ws/src/main.ts", "/ws/src/widgets"),     # resolves via dir/index.ts barrel
    )
    indexed = ("/ws/src/a.ts", "/ws/src/widgets/index.ts")
    assert _resolve_edge_confidence(edges, indexed) == (
        ("/ws/src/main.ts", "/ws/src/a.ts", "EXTRACTED", 1.0),
        ("/ws/src/main.ts", "/ws/src/widgets/index.ts", "EXTRACTED", 1.0),
    )
    # A bare (external) specifier has no resolvable file → stays INFERRED.
    assert _resolve_edge_confidence(
        (("/ws/src/main.ts", "react"),), ("/ws/src/a.ts",)
    ) == (("/ws/src/main.ts", "react", "INFERRED", 0.5),)


# ── POLY3 ─────────────────────────────────────────────────────────────────────


def test_poly3_workspace_escape_specifier_dropped() -> None:
    parent = _parse("import p from '../../../etc/passwd';\n", "typescript", "/ws/src/a.ts")
    assert _extract_ecmascript_imports(parent, _req("/ws/src/a.ts", "/ws")) == []
    # '/ws/src' + '../../ws_hacked/x' -> '/ws_hacked/x': shares the '/ws' textual
    # prefix but is NOT under the '/ws/' directory boundary.
    sibling = _parse("import p from '../../ws_hacked/x';\n", "typescript", "/ws/src/a.ts")
    assert _extract_ecmascript_imports(sibling, _req("/ws/src/a.ts", "/ws")) == []


# ── POLY4 ─────────────────────────────────────────────────────────────────────


def test_poly4_extraction_and_resolution_are_filesystem_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse("import a from './a';\nconst y = require('../lib/b');\n", "typescript", "/ws/src/a.ts")
    _extract_ecmascript_imports(tree, _req("/ws/src/a.ts", "/ws"))
    _resolve_edge_confidence((("/ws/src/main.ts", "/ws/src/a"),), ("/ws/src/a.ts",))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


# ── POLY5 ─────────────────────────────────────────────────────────────────────


def test_poly5_unregistered_language_yields_no_edges() -> None:
    assert IMPORT_EXTRACTORS.get("go") is None
    result = index_file_sync(
        IndexingRequest(
            file_path="/ws/main.go",
            content='package main\nimport "fmt"\n',
            language_id="go",
            workspace_root="/ws",
        )
    )
    assert result.success is True
    assert result.imports == []


# ── BLAST1 ────────────────────────────────────────────────────────────────────


def test_blast1_cycle_is_safe() -> None:
    edges = (("b", "a"), ("c", "b"), ("a", "c"))
    indexed = ("a", "b", "c")
    assert compute_blast_radius_sync(("a",), edges, indexed, depth=3) == ["b", "c"]


# ── BLAST2 ────────────────────────────────────────────────────────────────────


def test_blast2_stress_under_500ms() -> None:
    n = 5000
    indexed = tuple(f"/ws/f{i}.ts" for i in range(n))
    edges = tuple(
        (f"/ws/f{i}.ts", f"/ws/f{(i + k) % n}")
        for i in range(n)
        for k in (1, 2, 3)
    )  # 15 000 edges, each target extensionless → resolves via candidate expansion
    assert len(edges) == 15_000
    assert len(edges) < MAX_BLAST_EDGES  # cap not tripped

    start = time.perf_counter()
    radius = compute_blast_radius_sync(("/ws/f0.ts",), edges, indexed, depth=3)
    elapsed = time.perf_counter() - start

    assert radius, "the synthetic graph must produce a non-empty radius"
    assert elapsed < 0.5, f"blast radius took {elapsed:.3f}s (> 500 ms budget)"


def test_blast2_async_wrapper_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pure traversal runs in a worker thread — the loop stays responsive."""
    import core.blast_radius as br

    release = threading.Event()

    def _blocking(*_a: Any, **_k: Any) -> List[str]:
        release.wait(timeout=5)
        return ["dep"]

    monkeypatch.setattr(
        br.catalog_db, "get_all_edges", AsyncMock(return_value=[("/ws/main.ts", "/ws/a")])
    )
    monkeypatch.setattr(
        br.catalog_db, "list_indexed_files", AsyncMock(return_value=["/ws/a.ts"])
    )
    monkeypatch.setattr(br, "compute_blast_radius_sync", _blocking)

    async def _run() -> List[str]:
        # Call the import-bound real function (bypassing conftest's autouse
        # ``_stub_blast_radius``, which patches the ``br.`` module attribute); its
        # internal ``compute_blast_radius_sync`` / ``catalog_db`` lookups still resolve
        # through ``br``, so the monkeypatches above apply.
        task = asyncio.ensure_future(compute_blast_radius("proj", ["/ws/a.ts"]))
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        # The loop kept running while the sync traversal was blocked in its worker.
        assert ticks == 5
        assert not task.done(), "the traversal blocked the event loop (not offloaded)"
        release.set()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(_run()) == ["dep"]


# ── SNAP1 ─────────────────────────────────────────────────────────────────────


def test_snap1_round_trip_graph_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    ws = _n(str(tmp_path / "wsA"))
    (tmp_path / "wsA").mkdir(parents=True, exist_ok=True)
    proj = "proj_snap1"
    main_ts = _n(str(Path(ws) / "src" / "main.ts"))
    a_resolved = _n(str(Path(ws) / "src" / "a"))
    a_ts = _n(str(Path(ws) / "src" / "a.ts"))

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(main_ts, [a_resolved], proj)
        await catalog_db.upsert_edge_confidence([(main_ts, a_resolved, "EXTRACTED", 1.0)], proj)
        await catalog_db.upsert_ppr_scores({a_ts: 0.5}, proj, {a_ts: 7})

        pre_edges = sorted(await catalog_db.get_graph_edges_enriched(proj))
        pre_ppr = await catalog_db.get_ppr_scores_bulk([a_ts], proj)
        pre_comm = await catalog_db.get_community_ids_bulk([a_ts], proj)

        assert await ms.export_memory_snapshot(proj, ws) is not None

        _isolate_catalog(tmp_path, monkeypatch, "catalog2.sqlite")
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is True

        assert sorted(await catalog_db.get_graph_edges_enriched(proj)) == pre_edges
        assert await catalog_db.get_ppr_scores_bulk([a_ts], proj) == pre_ppr
        assert await catalog_db.get_community_ids_bulk([a_ts], proj) == pre_comm

    asyncio.run(_run())


# ── SNAP2 ─────────────────────────────────────────────────────────────────────


def test_snap2_concurrent_write_not_torn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    ws = _n(str(tmp_path / "wsC"))
    (tmp_path / "wsC").mkdir(parents=True, exist_ok=True)
    proj = "proj_snap2"
    src = _n(str(Path(ws) / "hub.py"))
    old = ["mod_x", "mod_y"]
    new = ["mod_p", "mod_q", "mod_r"]

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(src, old, proj)
        await asyncio.gather(
            ms.export_memory_snapshot(proj, ws),
            catalog_db.upsert_dependencies(src, new, proj),
        )
        _isolate_catalog(tmp_path, monkeypatch, "catalog_c.sqlite")
        await catalog_db.init_db()
        await ms.import_memory_snapshot(proj, ws)
        exported = {t for s, t in await catalog_db.get_all_edges(proj) if _n(s) == src}
        assert exported in (set(old), set(new)), f"torn read: {exported}"

    asyncio.run(_run())


# ── DEAD1 ─────────────────────────────────────────────────────────────────────


def test_dead1_allowlist_hardcoded_and_json_compose() -> None:
    indexed = (
        "/ws/tests/test_foo.py",        # hardcoded entrypoint (filename) → excluded
        "/ws/jobs/scheduled_task.py",   # JSON allowlist glob → excluded
        "/ws/pkg/orphan.py",            # plain orphan → flagged
    )
    result = compute_dead_code_sync(
        (), indexed, "/ws", ("jobs/*.py",), content_reader=_reader({})
    )
    assert result == [{"file": "pkg/orphan.py", "in_degree": 0}]


# ── DIGEST1 ───────────────────────────────────────────────────────────────────


def test_digest1_bounded_with_truthful_total_and_skeleton() -> None:
    files = tuple(f"mod{i:03d}/f.py" for i in range(_MODULE_LIMIT + 5))
    d: dict[str, Any] = build_architecture_digest_sync(
        rel_files=files, top_ppr_rel=(), community_ids=(), edge_count=0
    )
    assert len(d["top_modules"]["top"]) == _MODULE_LIMIT
    assert d["top_modules"]["total"] == _MODULE_LIMIT + 5

    empty = build_architecture_digest_sync(
        rel_files=(), top_ppr_rel=(), community_ids=(), edge_count=0
    )
    assert empty == _empty_digest()


# ── DIGEST2 ───────────────────────────────────────────────────────────────────


def _oversized_digest() -> dict[str, Any]:
    long = "x" * 400
    return {
        "languages": {"total": 30, "top": [{"language": long, "count": 1}] * 30},
        "top_modules": {"total": 30, "top": [{"module": long, "count": 1}] * 30},
        "hotspots": {"total": 30, "top": [{"file": long, "score": 0.1}] * 30},
        "community_clusters": {"count": 5, "largest": [{"id": 1, "size": 1}] * 30},
        "entrypoints": {"total": 30, "top": [long] * 30},
        "graph_schema": {"indexed_files": 30, "edges": 99},
    }


def test_digest2_token_capped_bounded_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.token_counter import PrecisionTokenCounter

    real = PrecisionTokenCounter.count
    calls = {"n": 0}

    def counting(text: str) -> int:
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(PrecisionTokenCounter, "count", staticmethod(counting))
    trimmed = _enforce_digest_token_cap(_oversized_digest())
    monkeypatch.undo()

    assert PrecisionTokenCounter.count(json.dumps(trimmed)) <= _DIGEST_TOKEN_CAP
    # One initial count + at most one per section dropped — never per-element.
    assert calls["n"] <= len(_DIGEST_TRIM_ORDER) + 1

    def run() -> dict[str, Any]:
        return build_architecture_digest_sync(
            rel_files=("core/a.py", "core/b.py", "brain/c.py", "main.py"),
            top_ppr_rel=(("core/a.py", 0.5), ("core/b.py", 0.5)),
            community_ids=(0, 0, 1),
            edge_count=3,
        )

    assert json.dumps(run()) == json.dumps(run())


# ── NOPOLLUTE1 ────────────────────────────────────────────────────────────────


def test_nopollute1_tier2_substrates_never_contaminate_file_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three Tier-2 tables are physically separate; the file-level analytics
    read only ``dependency_graph``. Seeding all three must not change what
    ``get_all_edges`` returns nor what ``compute_blast_radius`` traverses — a
    regression guard against a future merge of a Tier-2 table into the graph reads."""
    _isolate_catalog(tmp_path, monkeypatch)
    proj = "proj_nopollute"

    async def _run() -> None:
        await catalog_db.init_db()
        # File-level dependency_graph edge (main imports a) + indexed catalog.
        await catalog_db.upsert_dependencies("/ws/main.ts", ["/ws/a"], proj)
        for f in ("/ws/main.ts", "/ws/a.ts", "/ws/x.ts"):
            await catalog_db.upsert_indexed_file(f, proj)

        # Seed all three Tier-2 substrates, referencing the same files. The observed
        # edge's callee IS the blast-radius seed (/ws/a.ts): if observed edges leaked
        # into the reverse adjacency, /ws/x.ts would surface as a dependent.
        await catalog_db.persist_observed_edges(
            proj, [("x.fn", "/ws/x.ts", "a.fn", "/ws/a.ts")]
        )
        await catalog_db.replace_boundary_edges(
            proj, [("/ws/x.ts", "some_channel", "handles", "mcp")]
        )
        await catalog_db.upsert_symbol_definitions(
            "/ws/a.ts", [SymbolDef("a.fn", "function", 1, 2)], proj
        )

        # File-level reads are unaffected by the Tier-2 rows.
        assert await catalog_db.get_all_edges(proj) == [("/ws/main.ts", "/ws/a")]
        assert await compute_blast_radius(proj, ["/ws/a.ts"]) == ["/ws/main.ts"]

    asyncio.run(_run())
