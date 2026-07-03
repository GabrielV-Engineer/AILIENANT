"""core/call_trace_probe.py — offline sys.monitoring PoC harness.

Contracts under test:
  - CallTracer records intra-project caller->callee CALL edges and excludes out-of-root
    frames (venv/stdlib), via os-path-confined filtering.
  - Callee resolution unwraps `__wrapped__` chains so a decorated (functools.wraps) target
    maps to its own qualname/def-line, never the wrapper's.
  - ingest_edges canonicalizes to the catalog-comparable native-absolute form (NOT
    repo-relative — VFSMiddleware.read_safe opens file_path directly with no join against
    project_root, so the catalog's real convention is a directly-openable native path) and
    is idempotent by content_hash.
  - reconcile buckets observed edges against the existing find_symbol_callers resolver:
    confirmed / dynamic_discovery / unobserved — unobserved is NEVER phrased as dead.
  - The probe never touches dependency_graph/symbol_definitions as a side effect, and is
    not imported by any runtime module.
  - The sys.monitoring tool id is always freed — no residual global state.
  - The callback never raises on a malformed frame.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest

from core import db as catalog_db
from core.call_trace_probe import (
    CallTracer,
    ingest_edges,
    innermost_symbol,
    reconcile,
)
from shared.contracts import SymbolDef

_PID = "proj"


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))


async def _seed_native(
    tmp_path: Any,
    name: str,
    content: str,
    *,
    defs: "list[SymbolDef] | None" = None,
) -> str:
    """Write a real file and register it in the catalog under its native absolute path —
    the same string a traced ``co_filename`` produces for a module executed from disk
    (mirrors ``test_symbol_refs.py``'s ``_seed``, which stores ``str(p)`` for the same
    reason: ``find_symbol_callers``' VFS read needs a directly-openable key)."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    path = str(p)
    await catalog_db.upsert_indexed_file(path, _PID)
    await catalog_db.index_file_lines(path, content, _PID)
    if defs:
        await catalog_db.upsert_symbol_definitions(path, defs, _PID)
    return path


async def _snapshot_table(table: str) -> Any:
    import aiosqlite

    async with aiosqlite.connect(catalog_db.DB_CATALOG_PATH) as db:
        async with db.execute(f"SELECT * FROM {table}") as cur:
            return sorted(map(tuple, await cur.fetchall()))


# ── CallTracer: recording + root confinement ─────────────────────────────────


def test_tracer_records_intra_project_static_call(tmp_path: Any) -> None:
    callee_path = str(tmp_path / "callee_mod.py")
    caller_path = str(tmp_path / "caller_mod.py")
    callee_ns: Dict[str, Any] = {}
    exec(compile("def callee():\n    return 1\n", callee_path, "exec"), callee_ns)
    caller_ns: Dict[str, Any] = {}
    exec(compile("def caller(fn):\n    return fn()\n", caller_path, "exec"), caller_ns)

    tracer = CallTracer([str(tmp_path)])
    with tracer:
        caller_ns["caller"](callee_ns["callee"])

    hit = next(
        (
            (c, e) for (c, e) in tracer.edges
            if c[0] == caller_path and c[1] == "caller" and e[0] == callee_path and e[1] == "callee"
        ),
        None,
    )
    assert hit is not None, f"expected edge not found among {tracer.edges}"


def test_tracer_excludes_out_of_root_frame(tmp_path: Any) -> None:
    project_root = tmp_path / "project"
    excluded_root = project_root / "venv"
    excluded_root.mkdir(parents=True)

    caller_path = str(project_root / "caller.py")
    excluded_path = str(excluded_root / "vendored.py")
    excluded_ns: Dict[str, Any] = {}
    exec(compile("def vendored():\n    return 1\n", excluded_path, "exec"), excluded_ns)
    caller_ns: Dict[str, Any] = {}
    exec(compile("def caller(fn):\n    return fn()\n", caller_path, "exec"), caller_ns)

    tracer = CallTracer([str(project_root)], excludes=[str(excluded_root)])
    with tracer:
        caller_ns["caller"](excluded_ns["vendored"])

    assert not any(e[0] == excluded_path for (_c, e) in tracer.edges), (
        "an edge into the excluded (venv) root was recorded"
    )


def test_tool_id_freed_after_exit_leaves_no_residual_state(tmp_path: Any) -> None:
    tracer = CallTracer([str(tmp_path)])
    with tracer:
        used_id = next(
            (tid for tid in range(6) if sys.monitoring.get_tool(tid) == "ailienant_call_trace_probe"),
            None,
        )
        assert used_id is not None, "tracer did not register its tool id"
    assert sys.monitoring.get_tool(used_id) is None, "tool id was not freed on exit"

    # A second, independent trace must succeed cleanly — it would raise if the first
    # tracer's callback/events were never torn down (sys.monitoring is process-global).
    second = CallTracer([str(tmp_path)])
    with second:
        pass


def test_callback_never_raises_on_malformed_frame(tmp_path: Any) -> None:
    tracer = CallTracer([str(tmp_path)])

    class _Malformed:
        """No co_filename/co_qualname — simulates an unexpected frame shape."""

    result = tracer._on_call(_Malformed(), 0, _Malformed(), None)  # type: ignore[arg-type]
    assert result is None


# ── decorator-drift unwrap ────────────────────────────────────────────────────


def test_decorator_unwrap_maps_edge_to_wrapped_symbol_not_wrapper(tmp_path: Any) -> None:
    src = (
        "import functools\n"
        "def deco(f):\n"
        "    @functools.wraps(f)\n"
        "    def wrapper(*a, **k):\n"
        "        return f(*a, **k)\n"
        "    return wrapper\n"
        "\n"
        "@deco\n"
        "def target():\n"
        "    return 1\n"
    )
    path = str(tmp_path / "decorated_mod.py")
    ns: Dict[str, Any] = {}
    exec(compile(src, path, "exec"), ns)

    caller_path = str(tmp_path / "caller.py")
    caller_ns: Dict[str, Any] = {}
    exec(compile("def call_it(fn):\n    return fn()\n", caller_path, "exec"), caller_ns)

    tracer = CallTracer([str(tmp_path)])
    with tracer:
        caller_ns["call_it"](ns["target"])

    callees = {e for (_c, e) in tracer.edges if e[0] == path}
    assert callees, "no edge recorded for the decorated target"
    callee = next(iter(callees))
    assert callee[1] == "target", f"expected unwrapped qualname 'target', got {callee[1]!r}"
    # co_firstlineno for ANY decorated function (wrapped or not) points at the first
    # decorator line (empirically confirmed), not the `def` line — here line 8 (`@deco`),
    # one line above the catalog's tree-sitter-derived `def target():` at line 9.
    assert callee[2] == 8, f"expected the decorator line (8), got {callee[2]!r}"

    # The reconciler bridges that gap via innermost_symbol's bounded decorator fallback:
    # the catalog symbol (start_line = the `def` line, 9) must still resolve from the
    # traced decorator-line callee (8), or every decorated function would silently fail
    # to map to its catalog symbol.
    catalog_symbol = ("target", "function", 9, 10)
    assert innermost_symbol([catalog_symbol], callee[2]) == catalog_symbol


# ── innermost_symbol ──────────────────────────────────────────────────────────


def test_innermost_symbol_picks_method_over_class() -> None:
    symbols = [
        ("Service", "class", 1, 10),
        ("Service.handle", "method", 2, 4),
    ]
    result = innermost_symbol(symbols, 3)
    assert result is not None
    assert result[0] == "Service.handle"


# ── idempotent ingest ─────────────────────────────────────────────────────────


def test_idempotent_reingest_is_noop(tmp_path: Any) -> None:
    caller_path = str(tmp_path / "c.py")
    callee_path = str(tmp_path / "e.py")
    raw = [((caller_path, "caller", 1), (callee_path, "callee", 1))]

    report = ingest_edges(raw, str(tmp_path))
    assert report.ingested == 1
    assert report.duplicates == 0

    ingest_edges(raw, str(tmp_path), into=report)
    assert report.duplicates == 1
    assert len(report.edges) == 1, "re-ingesting the same artifact must not grow the set"


# ── reconciler: confirmed / dynamic-discovery / unobserved ───────────────────


def test_dynamic_dispatch_edge_is_dynamic_discovery(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A getattr-string dispatch has no identifier reference to the callee's name — the
    static resolver structurally cannot confirm it, so it is a dynamic discovery."""
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        target_src = "def handle_tool():\n    return 1\n"
        target_path = await _seed_native(
            tmp_path, "target_mod.py", target_src,
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        caller_src = (
            "def dispatch(mod):\n"
            "    fn = getattr(mod, 'handle_tool')\n"
            "    return fn()\n"
        )
        caller_path = await _seed_native(tmp_path, "dyn_caller.py", caller_src)

        target_mod = types.ModuleType("target_mod")
        exec(compile(target_src, target_path, "exec"), target_mod.__dict__)
        caller_ns: Dict[str, Any] = {}
        exec(compile(caller_src, caller_path, "exec"), caller_ns)

        tracer = CallTracer([str(tmp_path)])
        with tracer:
            caller_ns["dispatch"](target_mod)

        into = ingest_edges(list(tracer.edges), str(tmp_path))
        return await reconcile(into.edges, _PID, workspace_root=str(tmp_path))

    report = asyncio.run(_run())
    discovered = {qn for (_f, qn) in report.dynamic_discoveries}
    confirmed = {qn for (_f, qn) in report.confirmed}
    assert "handle_tool" in discovered
    assert "handle_tool" not in confirmed


def test_unobserved_static_candidate_reported_not_dead(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        target_src = "def handle_tool():\n    return 1\n"
        target_path = await _seed_native(
            tmp_path, "target_mod.py", target_src,
            defs=[SymbolDef("handle_tool", "function", 1, 2)],
        )
        # A real static reference — the resolver will confirm this file, but it is
        # NEVER executed, so it must surface as an unobserved candidate, not "dead".
        static_src = "def use_it():\n    return handle_tool()\n"
        static_path = await _seed_native(tmp_path, "static_caller.py", static_src)

        # A second caller, statically confirmable too, that we DO execute — keeps
        # `mapped` > 0 and exercises the confirmed branch alongside unobserved.
        other_src = "def other():\n    return handle_tool()\n"
        other_path = await _seed_native(tmp_path, "other_caller.py", other_src)

        target_mod = types.ModuleType("target_mod")
        exec(compile(target_src, target_path, "exec"), target_mod.__dict__)
        other_ns: Dict[str, Any] = {"handle_tool": target_mod.handle_tool}
        exec(compile(other_src, other_path, "exec"), other_ns)

        tracer = CallTracer([str(tmp_path)])
        with tracer:
            other_ns["other"]()

        into = ingest_edges(list(tracer.edges), str(tmp_path))
        return await reconcile(into.edges, _PID, workspace_root=str(tmp_path)), static_path

    report, static_path = asyncio.run(_run())
    unobserved_files = {f for (f, _qn) in report.unobserved_candidates}
    assert static_path in unobserved_files, "the never-executed static caller was dropped, not surfaced"
    # Structural guarantee: never phrased as "dead" anywhere in the report's shape.
    assert not any("dead" in name.lower() for name in vars(report))


async def _run_non_pollution_scenario(tmp_path: Any) -> Any:
    await catalog_db.init_db()
    target_src = "def handle_tool():\n    return 1\n"
    target_path = await _seed_native(
        tmp_path, "target_mod.py", target_src,
        defs=[SymbolDef("handle_tool", "function", 1, 2)],
    )
    caller_src = "def call_it():\n    return handle_tool()\n"
    caller_path = await _seed_native(tmp_path, "caller.py", caller_src)

    target_mod = types.ModuleType("target_mod")
    exec(compile(target_src, target_path, "exec"), target_mod.__dict__)
    caller_ns: Dict[str, Any] = {"handle_tool": target_mod.handle_tool}
    exec(compile(caller_src, caller_path, "exec"), caller_ns)

    before_deps = await _snapshot_table("dependency_graph")
    before_syms = await _snapshot_table("symbol_definitions")

    tracer = CallTracer([str(tmp_path)])
    with tracer:
        caller_ns["call_it"]()
    into = ingest_edges(list(tracer.edges), str(tmp_path))
    await reconcile(into.edges, _PID, workspace_root=str(tmp_path))

    after_deps = await _snapshot_table("dependency_graph")
    after_syms = await _snapshot_table("symbol_definitions")
    return before_deps, after_deps, before_syms, after_syms


def test_non_pollution_catalog_unchanged_after_trace(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    before_deps, after_deps, before_syms, after_syms = asyncio.run(
        _run_non_pollution_scenario(tmp_path)
    )
    assert before_deps == after_deps, "a trace + reconcile mutated dependency_graph"
    assert before_syms == after_syms, "a trace + reconcile mutated symbol_definitions"


def test_probe_not_imported_by_runtime_modules() -> None:
    root = Path(__file__).resolve().parent.parent
    hits = []
    for py in root.rglob("*.py"):
        parts = py.parts
        if "venv" in parts or "tests" in parts or py.name == "call_trace_probe.py":
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "call_trace_probe" in text:
            hits.append(str(py))
    assert hits == [], f"call_trace_probe referenced outside tests/itself: {hits}"
