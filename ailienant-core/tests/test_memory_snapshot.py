"""Shared memory snapshot — portable export/import of the dependency graph.

Verifies the DoD (round-trip graph equality, concurrent-writer snapshot isolation)
plus the hardening the design turns on: cross-clone portability (relativize +
re-key), zero-trust path confinement, a bounded-decompression zip-bomb refusal,
and idempotent ``.gitattributes`` maintenance.

Async cases run via ``asyncio.run`` (no pytest-asyncio); the catalog DB is isolated
per-test onto ``tmp_path`` through the ``DB_CATALOG_PATH`` seam, and every workspace
lives under ``tmp_path`` so the artifact and ``.gitattributes`` never touch the repo.
"""
import asyncio
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest
import zstandard as zstd

from core import db as catalog_db
from core import memory_snapshot as ms


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, name: str = "catalog.sqlite") -> str:
    db = str(tmp_path / name)
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def _n(p: str) -> str:
    return p.replace("\\", "/")


def _ws(tmp_path: Any, name: str) -> Tuple[str, str]:
    """Create a workspace dir under tmp_path; return (posix workspace_root, project_id)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return _n(str(root)), f"proj_{name}"


def _make_artifact(
    workspace_root: str,
    edge_rows: List[Tuple[str, str, int, Optional[str], Optional[float]]],
    ppr_rows: List[Tuple[str, float, Optional[int]]],
) -> Path:
    """Build a portable snapshot artifact by hand (white-box: lets a test inject
    escaping paths / opaque targets the public export would never emit)."""
    home = Path(workspace_root) / ".ailienant"
    home.mkdir(parents=True, exist_ok=True)
    tmp_db = home / "_build.db"
    conn = sqlite3.connect(str(tmp_db))
    try:
        for stmt in ms._PORTABLE_DDL:
            conn.execute(stmt)
        conn.executemany(
            "INSERT INTO graph_edges (source_rel, target, target_is_path, confidence, confidence_score) "
            "VALUES (?,?,?,?,?)",
            edge_rows,
        )
        conn.executemany(
            "INSERT INTO ppr (file_rel, ppr_score, leiden_community_id) VALUES (?,?,?)",
            ppr_rows,
        )
        conn.commit()
    finally:
        conn.close()
    blob = zstd.ZstdCompressor().compress(tmp_db.read_bytes())
    tmp_db.unlink()
    artifact = home / "memory.db.zst"
    artifact.write_bytes(blob)
    return artifact


# ── DoD: round-trip equality ─────────────────────────────────────────────────

def test_round_trip_graph_equality(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """export → (fresh catalog) → import reproduces the exact edge + PPR graph."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsA")
    main_ts = _n(str(Path(ws) / "src" / "main.ts"))
    a_resolved = _n(str(Path(ws) / "src" / "a"))          # resolved TS target (path)
    app_py = _n(str(Path(ws) / "pkg" / "app.py"))
    a_ts = _n(str(Path(ws) / "src" / "a.ts"))

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(main_ts, [a_resolved], proj)
        await catalog_db.upsert_dependencies(app_py, ["brain.state"], proj)  # opaque module
        await catalog_db.upsert_edge_confidence([(main_ts, a_resolved, "EXTRACTED", 1.0)], proj)
        await catalog_db.upsert_ppr_scores({a_ts: 0.5}, proj, {a_ts: 7})

        pre_edges = sorted(await catalog_db.get_graph_edges_enriched(proj))
        pre_ppr = await catalog_db.get_ppr_scores_bulk([a_ts], proj)
        pre_comm = await catalog_db.get_community_ids_bulk([a_ts], proj)

        assert await ms.export_memory_snapshot(proj, ws) is not None

        # Fresh, empty catalog — import must rebuild the graph identically.
        _isolate_catalog(tmp_path, monkeypatch, "catalog2.sqlite")
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is True

        assert sorted(await catalog_db.get_graph_edges_enriched(proj)) == pre_edges
        assert await catalog_db.get_ppr_scores_bulk([a_ts], proj) == pre_ppr
        assert await catalog_db.get_community_ids_bulk([a_ts], proj) == pre_comm

    asyncio.run(_run())


# ── Intent: cross-clone portability ──────────────────────────────────────────

def test_cross_path_portability(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot exported under one clone path imports live under a different one."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws_a, proj_a = _ws(tmp_path, "wsA")
    ws_b, proj_b = _ws(tmp_path, "wsB")
    main_a = _n(str(Path(ws_a) / "src" / "main.ts"))
    tgt_a = _n(str(Path(ws_a) / "src" / "a"))

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(main_a, [tgt_a], proj_a)
        await catalog_db.upsert_dependencies(_n(str(Path(ws_a) / "app.py")), ["brain.state"], proj_a)
        assert await ms.export_memory_snapshot(proj_a, ws_a) is not None

        # Copy the artifact into clone B and import under B's identity.
        (Path(ws_b) / ".ailienant").mkdir(parents=True, exist_ok=True)
        (Path(ws_b) / ".ailienant" / "memory.db.zst").write_bytes(
            (Path(ws_a) / ".ailienant" / "memory.db.zst").read_bytes()
        )
        _isolate_catalog(tmp_path, monkeypatch, "catalog_b.sqlite")
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj_b, ws_b) is True

        edges = set(await catalog_db.get_all_edges(proj_b))
        # Paths re-anchored to ws_b; the opaque module target survives verbatim.
        assert (_n(str(Path(ws_b) / "src" / "main.ts")), _n(str(Path(ws_b) / "src" / "a"))) in edges
        assert (_n(str(Path(ws_b) / "app.py")), "brain.state") in edges
        # Nothing leaked from clone A.
        assert not any("wsA" in s or "wsA" in t for s, t in edges)

    asyncio.run(_run())


# ── DoD: concurrent-writer snapshot isolation ────────────────────────────────

def test_concurrent_write_not_torn(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An export racing a source's DELETE+INSERT captures a whole edge set, never a blend.

    The catalog is WAL (via ``init_db``); the exporter reads under one
    ``BEGIN DEFERRED`` transaction, so a concurrent ``upsert_dependencies`` either
    lands wholly before the read snapshot or wholly after — the exported source's
    edges must equal exactly the old set or the new set.
    """
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsC")
    src = _n(str(Path(ws) / "hub.py"))
    old = ["mod_x", "mod_y"]
    new = ["mod_p", "mod_q", "mod_r"]  # opaque targets → no path mangling

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(src, old, proj)

        # Race the export against a full re-write of `src`'s edges.
        _, writer = await asyncio.gather(
            ms.export_memory_snapshot(proj, ws),
            catalog_db.upsert_dependencies(src, new, proj),
        )

        # Read the exported set back via a fresh-catalog import.
        _isolate_catalog(tmp_path, monkeypatch, "catalog_c.sqlite")
        await catalog_db.init_db()
        await ms.import_memory_snapshot(proj, ws)
        exported = {t for s, t in await catalog_db.get_all_edges(proj) if _n(s) == src}
        assert exported in (set(old), set(new)), f"torn read: {exported}"

    asyncio.run(_run())


# ── Guards & fail-open ───────────────────────────────────────────────────────

def test_import_no_op_when_graph_present(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live graph is never clobbered by a stale snapshot."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsD")

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(_n(str(Path(ws) / "a.py")), ["existing"], proj)
        await ms.export_memory_snapshot(proj, ws)  # artifact exists...
        # ...but the project already has a graph, so import is a no-op.
        assert await ms.import_memory_snapshot(proj, ws) is False
        assert (_n(str(Path(ws) / "a.py")), "existing") in await catalog_db.get_all_edges(proj)

    asyncio.run(_run())


def test_import_missing_artifact_returns_false(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsE")

    async def _run() -> None:
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is False

    asyncio.run(_run())


def test_import_garbage_artifact_fails_open(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated/garbage ``.zst`` returns False and raises nothing."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsF")
    home = Path(ws) / ".ailienant"
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory.db.zst").write_bytes(b"not a zstd stream at all")

    async def _run() -> None:
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is False

    asyncio.run(_run())


def test_zip_bomb_bound_refuses(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decompression past the ceiling is refused before it materializes."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsG")

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(_n(str(Path(ws) / "a.py")), ["mod"], proj)
        assert await ms.export_memory_snapshot(proj, ws) is not None
        # A tiny ceiling turns the (valid, >10-byte) artifact into a refusal.
        monkeypatch.setattr(ms, "MAX_DECOMPRESSED_BYTES", 10)
        _isolate_catalog(tmp_path, monkeypatch, "catalog_g.sqlite")
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is False
        assert await catalog_db.get_all_edges(proj) == []

    asyncio.run(_run())


# ── Zero-trust confinement & opaque targets ──────────────────────────────────

def test_path_escape_row_dropped(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A poisoned snapshot with an escaping path drops that row; in-tree rows import."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsH")
    _make_artifact(
        ws,
        edge_rows=[
            ("../../evil.py", "mod_evil", 0, None, None),   # escapes workspace → dropped
            ("src/ok.py", "mod_ok", 0, None, None),         # in-tree → kept
        ],
        ppr_rows=[("../../evil.py", 0.9, None), ("src/ok.py", 0.1, 3)],
    )

    async def _run() -> None:
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is True
        edges = await catalog_db.get_all_edges(proj)
        assert (_n(str(Path(ws) / "src" / "ok.py")), "mod_ok") in edges
        assert not any("evil" in s or "evil" in t for s, t in edges)

    asyncio.run(_run())


def test_opaque_module_target_round_trips(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dotted module target (target_is_path=0) is restored verbatim, not path-mangled."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsI")
    _make_artifact(
        ws,
        edge_rows=[("pkg/app.py", "brain.state.manager", 0, "INFERRED", 0.5)],
        ppr_rows=[],
    )

    async def _run() -> None:
        await catalog_db.init_db()
        assert await ms.import_memory_snapshot(proj, ws) is True
        edges = await catalog_db.get_all_edges(proj)
        assert (_n(str(Path(ws) / "pkg" / "app.py")), "brain.state.manager") in edges

    asyncio.run(_run())


# ── .gitattributes maintenance ───────────────────────────────────────────────

def test_gitattributes_written_and_idempotent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Export adds the merge=ours rule once; a second export does not duplicate it,
    and any pre-existing content is preserved."""
    _isolate_catalog(tmp_path, monkeypatch)
    ws, proj = _ws(tmp_path, "wsJ")
    ga = Path(ws) / ".gitattributes"
    ga.write_text("*.md text\n", encoding="utf-8")

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_dependencies(_n(str(Path(ws) / "a.py")), ["mod"], proj)
        await ms.export_memory_snapshot(proj, ws)
        await ms.export_memory_snapshot(proj, ws)  # second pass — must not duplicate
        text = ga.read_text(encoding="utf-8")
        assert "*.md text" in text  # pre-existing content preserved
        assert text.count(ms._GITATTRIBUTES_LINE) == 1

    asyncio.run(_run())
