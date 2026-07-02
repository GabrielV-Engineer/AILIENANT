"""Tier-2 symbol substrate: catalog population + lazy two-pass caller resolution.

Contracts under test:
  - collect_symbol_defs builds dotted FQNs (module → class → method), 1-indexed.
  - upsert_symbol_definitions replaces a file's rows on reindex (no orphan).
  - find_symbol_callers confirms real identifier references and rejects string/comment
    noise and the definition site itself.
  - Import-scoped resolution RANKS but never DISCARDS: a dynamic-dispatch caller with no
    import edge (modelled on ToolDispatcher's string-keyed lookup) is still returned,
    tagged INFERRED — a hard import gate would silently drop it.
  - An uncatalogued/empty result is never phrased as "dead".
  - A non-identifier symbol name is refused before any search (injection/ReDoS guard).
  - The post-ranking cap raises a truthful `truncated` flag.
  - The tool is reachable, READ_ONLY, via both build_analyst_tools and build_researcher_tools.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

import pytest

from core import blast_radius as _blast_radius_module
from core import db as catalog_db
from core.ast_engine import ASTEngine, collect_symbol_defs
from core.blast_radius import compute_blast_radius as _real_compute_blast_radius
from core.permissions import ToolPrivilegeTier
from shared.contracts import SymbolDef

_PID = "proj"


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))


async def _seed(
    tmp_path: Any,
    name: str,
    content: str,
    *,
    defs: Optional[List[SymbolDef]] = None,
    deps: Optional[List[str]] = None,
) -> str:
    """Write a real file and register it across the catalog tables. Returns its path."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    path = str(p)
    await catalog_db.upsert_indexed_file(path, _PID)
    await catalog_db.index_file_lines(path, content, _PID)
    if defs:
        await catalog_db.upsert_symbol_definitions(path, defs, _PID)
    if deps:
        await catalog_db.upsert_dependencies(path, deps, _PID)
    return path


# ── collect_symbol_defs (pure extraction) ─────────────────────────────────────


def test_collect_symbol_defs_builds_dotted_fqns() -> None:
    src = (
        "def top_level():\n"
        "    pass\n"
        "\n"
        "class Service:\n"
        "    def handle(self):\n"
        "        pass\n"
        "\n"
        "    class Inner:\n"
        "        def deep(self):\n"
        "            pass\n"
    )
    tree = ASTEngine().parse("m.py", src, "python")
    assert tree is not None
    got = {(fqn, kind) for fqn, kind, _s, _e in collect_symbol_defs(tree.root_node)}
    assert ("top_level", "function") in got
    assert ("Service", "class") in got
    assert ("Service.handle", "method") in got
    assert ("Service.Inner", "class") in got
    assert ("Service.Inner.deep", "method") in got


def test_collect_symbol_defs_lines_are_1_indexed() -> None:
    tree = ASTEngine().parse("m.py", "def a():\n    pass\n", "python")
    assert tree is not None
    rows = collect_symbol_defs(tree.root_node)
    assert rows and rows[0][2] == 1  # start_line of a def on the first source line


# ── catalog population + per-file replace ─────────────────────────────────────


def test_upsert_symbol_definitions_replaces_per_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> List[Any]:
        await catalog_db.init_db()
        await catalog_db.upsert_symbol_definitions(
            "f.py", [SymbolDef("old_fn", "function", 1, 2)], _PID
        )
        # Reindex the same file with a different symbol set → old rows must vanish.
        await catalog_db.upsert_symbol_definitions(
            "f.py", [SymbolDef("new_fn", "function", 1, 2)], _PID
        )
        stale = await catalog_db.get_symbol_definitions(_PID, "old_fn")
        fresh = await catalog_db.get_symbol_definitions(_PID, "new_fn")
        return [stale, fresh]

    stale, fresh = asyncio.run(_run())
    assert stale == []           # replaced, not accumulated
    assert fresh and fresh[0][0] == "f.py"


# ── find_symbol_callers: the two-pass resolution ──────────────────────────────


def test_dynamic_dispatch_caller_is_inferred_not_dropped(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decisive test: a caller referencing the symbol with NO import edge (modelled
    on ToolDispatcher's string-keyed dict) is still returned, tagged INFERRED."""
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed(
            tmp_path, "target_mod.py",
            "def handle_tool():\n    return 1\n",
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        # Caller references handle_tool via a string-keyed registry, no import edge.
        await _seed(
            tmp_path, "dyn_caller.py",
            "registry = {'h': handle_tool}\n",
        )
        return await find_symbol_callers(
            "handle_tool", _PID, workspace_root=str(tmp_path)
        )

    result = asyncio.run(_run())
    callers = {c.file_path.replace("\\", "/"): c.confidence for c in result.callers}
    hit = next((k for k in callers if k.endswith("dyn_caller.py")), None)
    assert hit is not None, "dynamic-dispatch caller was dropped — the import-gate bug"
    assert callers[hit] == "INFERRED"


def test_importing_caller_is_extracted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    # conftest's autouse _stub_blast_radius stubs compute_blast_radius to [] for
    # hermetic units elsewhere; this test specifically exercises the real resolution
    # against the isolated on-disk catalog, so it restores the genuine function
    # (captured at module-import time, before the stub fixture ever runs).
    monkeypatch.setattr(_blast_radius_module, "compute_blast_radius", _real_compute_blast_radius)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed(
            tmp_path, "target_mod.py",
            "def handle_tool():\n    return 1\n",
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        # Caller imports the defining module (edge resolves via the py suffix index).
        await _seed(
            tmp_path, "importer.py",
            "from target_mod import handle_tool\nhandle_tool()\n",
            deps=["target_mod"],
        )
        return await find_symbol_callers(
            "handle_tool", _PID, workspace_root=str(tmp_path)
        )

    result = asyncio.run(_run())
    tiers = {c.file_path.replace("\\", "/").split("/")[-1]: c.confidence for c in result.callers}
    assert tiers.get("importer.py") == "EXTRACTED"


def test_string_and_comment_noise_is_rejected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed(
            tmp_path, "target_mod.py",
            "def handle_tool():\n    return 1\n",
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        # Only a string literal and a comment mention the name — no real reference.
        await _seed(
            tmp_path, "noise.py",
            "x = 'call handle_tool here'\n# handle_tool is nice\n",
        )
        return await find_symbol_callers(
            "handle_tool", _PID, workspace_root=str(tmp_path)
        )

    result = asyncio.run(_run())
    names = {c.file_path.replace("\\", "/").split("/")[-1] for c in result.callers}
    assert "noise.py" not in names  # string/comment mentions are not identifier refs


def test_definition_site_is_not_a_caller(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        # The defining file mentions the name ONLY at its definition — not a caller.
        await _seed(
            tmp_path, "solo.py",
            "def handle_tool():\n    return 1\n",
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        return await find_symbol_callers(
            "handle_tool", _PID, workspace_root=str(tmp_path)
        )

    result = asyncio.run(_run())
    names = {c.file_path.replace("\\", "/").split("/")[-1] for c in result.callers}
    assert "solo.py" not in names


def test_uncatalogued_symbol_is_not_dead(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        return await find_symbol_callers("never_defined", _PID, workspace_root=str(tmp_path))

    result = asyncio.run(_run())
    assert result.in_catalog is False
    assert result.defined_in == []


@pytest.mark.parametrize("bad", ["foo; DROP TABLE", "../etc/passwd", "a b", "a-b", ""])
def test_non_identifier_name_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        return await find_symbol_callers(bad, _PID, workspace_root=str(tmp_path))

    with pytest.raises(ValueError):
        asyncio.run(_run())


def test_post_ranking_cap_sets_truncated(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    from core.symbol_refs import find_symbol_callers

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed(
            tmp_path, "target_mod.py",
            "def handle_tool():\n    return 1\n",
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        for i in range(4):
            await _seed(tmp_path, f"c{i}.py", "y = handle_tool()\n")
        return await find_symbol_callers(
            "handle_tool", _PID, workspace_root=str(tmp_path), max_results=2
        )

    result = asyncio.run(_run())
    assert len(result.callers) == 2
    assert result.truncated is True


# ── tool reachability + privilege tier ────────────────────────────────────────


def test_tool_reachable_read_only_via_both_builds() -> None:
    from tools.analyst_tools import build_analyst_tools
    from tools.perception_tools import FindSymbolCallersTool
    from tools.researcher_tools import build_researcher_tools

    state = {
        "workspace_root": "/w",
        "project_id": "p",
        "session_id": "s",
        "task_id": "t",
    }
    for build in (build_analyst_tools, build_researcher_tools):
        reg = build(state).get("find_symbol_callers")
        assert reg is not None, f"{build.__name__} did not wire find_symbol_callers"
        assert isinstance(reg.tool, FindSymbolCallersTool)
        assert reg.tier == ToolPrivilegeTier.READ_ONLY


def test_tool_output_never_says_dead_on_empty(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    from tools.perception_tools import FindSymbolCallersTool

    async def _run() -> str:
        await catalog_db.init_db()
        tool = FindSymbolCallersTool(project_id=_PID, workspace_root=str(tmp_path))
        return await tool._arun(symbol_name="never_defined")

    payload = json.loads(asyncio.run(_run()))
    assert payload["callers"] == []
    assert "dead" not in json.dumps(payload).lower() or "NOT" in payload.get("note", "")
    assert payload["in_catalog"] is False
