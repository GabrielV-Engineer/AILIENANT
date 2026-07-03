"""Offline validation harness: measure whether runtime call traces sharpen the static
call-graph signal, without persisting anything or touching a runtime path.

The static substrate stores no call edges — callers are resolved lazily by
:func:`core.symbol_refs.find_symbol_callers` and tagged by confidence tier. This harness
captures intra-project ``caller -> callee`` edges at runtime via :mod:`sys.monitoring`
(PEP 669) and reconciles them against that resolver to answer one question empirically:
do observed edges surface real callers the static path structurally misses (dynamic
dispatch), enough to justify a persisted trace substrate?

Nothing here is imported by a runtime path. The reconciler reads the catalog but writes
nothing; a trace never mutates ``dependency_graph`` or ``symbol_definitions``.

Buckets are file-granular (the resolver's native unit):
  * confirmed          — observed caller file the static resolver already finds.
  * dynamic_discovery  — observed caller file the resolver misses (the value signal).
  * unobserved         — static caller file never seen at runtime. Reported as
                         ``unobserved``, NEVER ``dead``: partial coverage means
                         never-observed is not proof of deadness — traces only raise
                         confidence, they never delete an edge.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# (raw absolute co_filename, co_qualname, co_firstlineno). Filenames stay raw through the
# hot path; canonicalization to the catalog-comparable native-absolute form happens at
# ingest (see normalize_path).
Endpoint = Tuple[str, str, int]
Edge = Tuple[Endpoint, Endpoint]

# A callable can nest wrappers arbitrarily; bound the unwrap so a pathological
# self-referential ``__wrapped__`` can never spin.
_MAX_UNWRAP: int = 20


# ── callee resolution ────────────────────────────────────────────────────────


def _callee_code(callable_obj: Any) -> Optional[CodeType]:
    """The underlying Python code object a call targets, or ``None`` for a C callable.

    Resolves a bound method to its function, then unwraps ``functools.wraps`` chains —
    load-bearing because ``sys.monitoring`` observes the *wrapper*, whose ``co_firstlineno``
    points into the decorator's body (a wrapper mis-maps to the decorator's symbol). The
    loop restores both the real ``co_qualname`` and the real ``def`` line. Decorators that
    do not set ``__wrapped__`` (not ``functools.wraps``-based) cannot be unwrapped — a
    bounded, documented mis-attribution, never a crash.
    """
    target = getattr(callable_obj, "__func__", callable_obj)  # bound/class method -> function
    seen = 0
    while seen < _MAX_UNWRAP:
        wrapped = getattr(target, "__wrapped__", None)
        if wrapped is None:
            break
        target = getattr(wrapped, "__func__", wrapped)
        seen += 1
    return getattr(target, "__code__", None)


# ── tracer ───────────────────────────────────────────────────────────────────


def _acquire_tool_id() -> int:
    """First free ``sys.monitoring`` tool id, guarding against a slot already claimed by
    another tool (e.g. ``pytest-cov``'s sysmon backend)."""
    for tid in range(6):
        if sys.monitoring.get_tool(tid) is None:
            return tid
    raise RuntimeError("no free sys.monitoring tool id (all 6 in use)")


class CallTracer:
    """Context manager recording intra-project ``caller -> callee`` edges via CALL events.

    ``roots`` are the include prefixes (project source); ``excludes`` are prefixes to drop
    even when under a root (e.g. the vendored ``venv``). ``sys.monitoring`` is a *global*
    interpreter facility, so the tool id is always freed in ``__exit__`` — a leaked callback
    would corrupt every subsequent trace. Presence, not count, is the signal, so a recorded
    call-site returns ``DISABLE`` to stop re-firing (bounds overhead on a full test slice).
    """

    def __init__(self, roots: Sequence[str], excludes: Sequence[str] = ()) -> None:
        self._roots = tuple(os.path.normcase(os.path.abspath(r)) for r in roots)
        self._excludes = tuple(os.path.normcase(os.path.abspath(e)) for e in excludes)
        self.edges: Set[Edge] = set()
        self._tool_id: Optional[int] = None

    def _under_root(self, filename: str) -> bool:
        try:
            norm = os.path.normcase(os.path.abspath(filename))
        except (ValueError, OSError):
            return False
        if any(norm.startswith(x) for x in self._excludes):
            return False
        return any(norm.startswith(r) for r in self._roots)

    def _on_call(self, code: CodeType, offset: int, callable_obj: Any, arg0: Any) -> Any:
        disable = sys.monitoring.DISABLE
        try:
            # A non-project call site never originates a project edge — retire it.
            if not self._under_root(code.co_filename):
                return disable
            callee = _callee_code(callable_obj)
            if callee is None or not self._under_root(callee.co_filename):
                # Project caller, off-project callee this time. Keep the site live: a
                # polymorphic site may still dispatch to a project callee later.
                return None
            self.edges.add((
                (code.co_filename, code.co_qualname, code.co_firstlineno),
                (callee.co_filename, callee.co_qualname, callee.co_firstlineno),
            ))
            return disable
        except Exception:  # noqa: BLE001 — one bad frame must never abort a global trace
            logger.debug("call_trace_probe: skipped a frame", exc_info=True)
            return None

    def __enter__(self) -> "CallTracer":
        mon = sys.monitoring
        self._tool_id = _acquire_tool_id()
        mon.use_tool_id(self._tool_id, "ailienant_call_trace_probe")
        mon.register_callback(self._tool_id, mon.events.CALL, self._on_call)
        mon.set_events(self._tool_id, mon.events.CALL)
        return self

    def __exit__(self, *exc: Any) -> None:
        mon = sys.monitoring
        tid = self._tool_id
        if tid is None:
            return
        try:
            mon.set_events(tid, 0)
            mon.register_callback(tid, mon.events.CALL, None)
        finally:
            mon.free_tool_id(tid)
            self._tool_id = None


# ── normalization + mapping ──────────────────────────────────────────────────


def normalize_path(raw_filename: str, repo_root: str) -> Optional[str]:
    """A raw ``co_filename`` canonicalized to the catalog's ``file_path`` convention, or
    ``None`` when the file does not resolve under ``repo_root``.

    The catalog stores a **directly-openable native path** (confirmed against
    :func:`core.vfs_middleware.VFSMiddleware.read_safe`, which calls ``open()`` on
    ``file_path`` with no join against ``project_root`` — the exact convention
    ``test_symbol_refs.py``'s ``_seed`` helper uses: ``str(p)``, native absolute). It is
    **not** repo-relative, and forward-slash normalization is applied only ad-hoc, at
    comparison sites (mirroring ``symbol_refs.py``'s own ``path.replace("\\\\", "/")``
    before set-membership checks) — never by stripping the path to relative. So this
    function canonicalizes via ``os.path.normpath``/``os.path.abspath`` (stable regardless
    of path-separator or ``.``/``..`` spelling) and re-confines to ``repo_root``, but does
    **not** reduce to a relative path — doing so would silently break every catalog lookup,
    since :func:`core.db.get_symbols_in_file` is an exact string match against the stored key.
    """
    try:
        resolved = os.path.normpath(os.path.abspath(raw_filename))
        root = os.path.normpath(os.path.abspath(repo_root))
    except (ValueError, OSError):
        return None
    if not os.path.normcase(resolved).startswith(os.path.normcase(root)):
        return None
    return resolved


# A catalog symbol row: (qualified_name, kind, start_line, end_line).
Symbol = Tuple[str, str, int, int]


_MAX_DECORATOR_GAP: int = 20


def innermost_symbol(symbols: Sequence[Symbol], line: int) -> Optional[Symbol]:
    """The symbol ``line`` belongs to, preferring the tightest containing span.

    Minimizing the span resolves a method to the method (not its enclosing class) and a
    line inside a def to that def rather than the module.

    **Decorator gap (empirically confirmed):** ``co_firstlineno`` for *any* decorated
    function — with or without ``functools.wraps`` — points at the **first decorator
    line**, not the ``def`` line, while :func:`core.ast_engine.collect_symbol_defs` records
    ``start_line`` as the tree-sitter ``function_definition`` span, which **excludes** the
    decorator. So a decorated symbol's traced callee line falls a few lines *above*
    ``start_line`` — outside strict containment — for every decorated function in this
    codebase (``@pytest.fixture``, LangGraph node decorators, etc.), not just wrapped ones.
    When no symbol contains ``line`` outright, fall back to the nearest symbol whose
    ``start_line`` is within ``_MAX_DECORATOR_GAP`` lines *after* ``line`` — bounded so an
    unrelated, far-away symbol is never mistaken for a genuine decorator prefix.
    """
    best: Optional[Symbol] = None
    best_key: Optional[Tuple[int, int]] = None
    for sym in symbols:
        _qn, _kind, start, end = sym
        if start <= line <= end:
            key = (0, end - start)
        elif start > line and (start - line) <= _MAX_DECORATOR_GAP:
            key = (1, start - line)
        else:
            continue
        if best is None or key < best_key:  # type: ignore[operator]
            best, best_key = sym, key
    return best


# ── idempotent ingest ────────────────────────────────────────────────────────


def content_hash(edge: Edge) -> str:
    """Stable identity of a normalized edge — the dedup key that makes re-ingesting the
    same serialized trace a no-op (the meaningful idempotency unit once traces are files)."""
    payload = json.dumps(edge, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class IngestReport:
    """Outcome of folding raw traced edges into the normalized accumulator."""
    ingested: int = 0          # edges that normalized and were newly added
    duplicates: int = 0        # edges already present (idempotent re-ingest)
    dropped: int = 0           # endpoints that did not normalize under the repo root
    edges: Set[Edge] = field(default_factory=set)          # normalized, catalog-comparable
    seen: Set[str] = field(default_factory=set)            # content hashes


def ingest_edges(
    raw_edges: Sequence[Edge], repo_root: str, *, into: Optional[IngestReport] = None
) -> IngestReport:
    """Canonicalize endpoints to the catalog-comparable native-absolute form (see
    :func:`normalize_path`) and fold them into ``into`` (or a fresh report), deduped by
    :func:`content_hash`. Re-ingesting the same edges only bumps ``duplicates`` — verified
    idempotent, no persistence involved."""
    report = into if into is not None else IngestReport()
    for (caller, callee) in raw_edges:
        c_file = normalize_path(caller[0], repo_root)
        e_file = normalize_path(callee[0], repo_root)
        if c_file is None or e_file is None:
            report.dropped += 1
            continue
        norm: Edge = ((c_file, caller[1], caller[2]), (e_file, callee[1], callee[2]))
        h = content_hash(norm)
        if h in report.seen:
            report.duplicates += 1
            continue
        report.seen.add(h)
        report.edges.add(norm)
        report.ingested += 1
    return report


# ── reconciler ───────────────────────────────────────────────────────────────


@dataclass
class CoverageReport:
    """Coverage of the runtime signal against the static resolver. See module docstring.

    ``unobserved`` is reported verbatim and MUST NOT be read as ``dead``.
    """
    observed: int = 0                       # normalized runtime edges considered
    mapped: int = 0                         # edges whose callee resolved to a catalog symbol
    confirmed: List[Tuple[str, str]] = field(default_factory=list)           # (caller_file, callee_qn)
    dynamic_discoveries: List[Tuple[str, str]] = field(default_factory=list)  # (caller_file, callee_qn)
    unobserved_candidates: List[Tuple[str, str]] = field(default_factory=list)  # (caller_file, callee_qn)

    @property
    def go(self) -> bool:
        """GO when runtime traces surface real callers the static path misses."""
        return len(self.dynamic_discoveries) > 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "observed": self.observed,
            "mapped": self.mapped,
            "confirmed": len(self.confirmed),
            "dynamic_discoveries": len(self.dynamic_discoveries),
            "unobserved_candidates": len(self.unobserved_candidates),
            "recommendation": "GO" if self.go else "NO-GO",
        }


async def reconcile(
    edges: Set[Edge], project_id: str, *, workspace_root: str = ""
) -> CoverageReport:
    """Bucket normalized runtime edges against the static resolver.

    For each distinct callee: map its ``(file, firstlineno)`` to the innermost catalog
    symbol, ask :func:`find_symbol_callers` for that symbol's caller files, then classify
    every observed caller as confirmed vs dynamic-discovery, and every unobserved resolver
    caller as an ``unobserved`` candidate (never ``dead``).
    """
    from core.db import get_symbols_in_file
    from core.symbol_refs import find_symbol_callers

    report = CoverageReport(observed=len(edges))

    # Group observed caller files by callee endpoint (dedup resolver work per callee).
    callee_to_callers: Dict[Endpoint, Set[str]] = {}
    for (caller, callee) in edges:
        callee_to_callers.setdefault(callee, set()).add(caller[0])

    # Cache per-file symbol lists — a callee file recurs across many edges.
    file_symbols: Dict[str, List[Symbol]] = {}

    for callee_ep, observed_callers in callee_to_callers.items():
        callee_file, _callee_qn, callee_line = callee_ep
        if callee_file not in file_symbols:
            file_symbols[callee_file] = list(
                await get_symbols_in_file(project_id, callee_file)
            )
        sym = innermost_symbol(file_symbols[callee_file], callee_line)
        if sym is None:
            continue  # runtime callee not in the catalog (uncatalogued) — cannot reconcile
        callee_qn = sym[0]
        report.mapped += len(observed_callers)

        result = await find_symbol_callers(
            callee_qn, project_id, workspace_root=workspace_root
        )
        # Comparison-only slash normalization (mirrors symbol_refs.py's own
        # `path.replace("\\", "/")` at its membership checks) — never the storage key,
        # since get_symbols_in_file above needed the raw catalog-matching form.
        static_by_norm: Dict[str, str] = {
            c.file_path.replace("\\", "/"): c.file_path for c in result.callers
        }
        observed_by_norm: Dict[str, str] = {
            f.replace("\\", "/"): f for f in observed_callers
        }

        for norm, caller_file in observed_by_norm.items():
            key = (caller_file, callee_qn)
            if norm in static_by_norm:
                report.confirmed.append(key)
            else:
                report.dynamic_discoveries.append(key)

        # Static candidates never exercised at runtime — surfaced, never called dead.
        for norm in sorted(set(static_by_norm) - set(observed_by_norm)):
            report.unobserved_candidates.append((static_by_norm[norm], callee_qn))

    return report


# ── dogfood entrypoint (one-off measurement; not a CI gate) ──────────────────


def _iter_py_files(base: Path, subdirs: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    for sub in subdirs:
        root = base / sub
        if root.is_dir():
            files.extend(p for p in root.rglob("*.py") if "venv" not in p.parts)
    return files


async def _index_source(base: Path, subdirs: Sequence[str], project_id: str) -> int:
    """Populate ``symbol_definitions`` (+ line index) for the traced source so the
    resolver has a catalog to answer against.

    Paths are stored as the **native absolute** form (``str(path.resolve())``) — the same
    string a traced ``co_filename`` naturally produces at runtime for a module executed
    from disk, and the convention ``find_symbol_callers``'s VFS read actually requires
    (see :func:`normalize_path`'s docstring). A repo-relative key would silently never
    match either the traced edges or a real disk read.
    """
    from core.ast_engine import ASTEngine, collect_symbol_defs
    from core.db import index_file_lines, upsert_indexed_file, upsert_symbol_definitions
    from shared.contracts import SymbolDef, detect_language

    engine = ASTEngine()
    count = 0
    for path in _iter_py_files(base, subdirs):
        abs_path = str(path.resolve())
        content = path.read_text(encoding="utf-8", errors="replace")
        lang = detect_language(abs_path)
        if not lang:
            continue
        tree = engine.parse(abs_path, content, lang)
        if tree is None:
            continue
        defs = [
            SymbolDef(qualified_name=q, kind=k, start_line=s, end_line=e)
            for (q, k, s, e) in collect_symbol_defs(tree.root_node)
        ]
        await upsert_indexed_file(abs_path, project_id)
        await index_file_lines(abs_path, content, project_id)
        await upsert_symbol_definitions(abs_path, defs, project_id)
        count += 1
    return count


def _child_trace(jsonl_out: str, roots: Sequence[str], excludes: Sequence[str], pytest_args: Sequence[str]) -> int:
    """Run pytest under the tracer inside this (child) process and dump edges to JSONL."""
    import pytest

    tracer = CallTracer(roots, excludes)
    with tracer:
        pytest.main(list(pytest_args))
    with open(jsonl_out, "w", encoding="utf-8") as fh:
        for edge in tracer.edges:
            fh.write(json.dumps(edge) + "\n")
    return len(tracer.edges)


def _read_jsonl(path: str) -> List[Edge]:
    edges: List[Edge] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            caller, callee = json.loads(line)
            edges.append(((caller[0], caller[1], caller[2]), (callee[0], callee[1], callee[2])))
    return edges


async def run_dogfood(
    base: Path,
    *,
    subdirs: Sequence[str] = ("core", "brain", "tools"),
    pytest_slice: Sequence[str] = (),
    project_id: str = "call_trace_dogfood",
) -> CoverageReport:
    """Index the source, trace a pytest slice in a subprocess, reconcile, and report.

    The catalog is isolated to a temp DB so the measurement never touches the real one.
    """
    import subprocess
    import tempfile

    from core import db as catalog_db

    workdir = Path(tempfile.mkdtemp(prefix="call_trace_probe_"))
    catalog_db.DB_CATALOG_PATH = str(workdir / "catalog.sqlite")
    await catalog_db.init_db()

    indexed = await _index_source(base, subdirs, project_id)
    logger.info("dogfood: indexed %d source files", indexed)

    jsonl_out = str(workdir / "trace.jsonl")
    excludes = [str(base / "venv")]
    child_cmd = [
        sys.executable, "-m", "core.call_trace_probe", "--child",
        jsonl_out, str(base), *excludes, "--", *pytest_slice,
    ]
    subprocess.run(child_cmd, cwd=str(base), check=False)

    into = ingest_edges(_read_jsonl(jsonl_out), str(base))
    report = await reconcile(into.edges, project_id, workspace_root=str(base))
    logger.info("dogfood: %s", report.as_dict())
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m core.call_trace_probe``: parent runs the measurement; ``--child`` traces."""
    import asyncio

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--child":
        rest = args[1:]
        sep = rest.index("--")
        jsonl_out, roots_excludes = rest[0], rest[1:sep]
        pytest_args = rest[sep + 1:]
        roots = roots_excludes[:1]
        excludes = roots_excludes[1:]
        _child_trace(jsonl_out, roots, excludes, pytest_args)
        return 0

    base = Path(__file__).resolve().parent.parent
    pytest_slice = args or [
        "tests/test_micro_swarm_e2e.py",
        "tests/test_phase8_8_2_analyst_arsenal.py",
        "-q", "-p", "no:cacheprovider",
    ]
    report = asyncio.run(run_dogfood(base, pytest_slice=pytest_slice))
    print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
