"""
brain/memory.py — Process-pool-safe indexing bridge.

All functions are module-level so ProcessPoolExecutor can pickle them.
Phase 3 extends this module with LanceDB vector indexing and GraphRAG topology extraction.
"""
from __future__ import annotations

import logging
import os
import posixpath
from collections import Counter
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from shared.contracts import IndexingRequest, IndexingResult, PPRRequest, PPRResult

logger = logging.getLogger("MEMORY_WORKER")

# Upper bound on the dependency-graph edge count a single PPR / analytics call
# will build. networkx is pure-Python (dict-of-dict-of-dict) with large per-node
# and per-edge heap overhead, and the undirected projection briefly doubles the
# structure — refusing oversized graphs caps the transient heap spike per call so
# a pathologically large workspace cannot stall the pooled worker. Gating on the
# edge count keeps the check O(1) and pre-build (the node count, on the order of
# the edge count for a sparse dependency graph, is only known after building).
MAX_GRAPH_EDGES: int = 5000

# Per-process singleton — initialized once by _worker_init(), never shared across processes.
_worker_ast: Optional[Any] = None


def _worker_init() -> None:
    """Called once per worker process by ProcessPoolExecutor(initializer=_worker_init)."""
    global _worker_ast
    from core.ast_engine import ASTEngine
    _worker_ast = ASTEngine()


def _count_top_level_symbols(tree: Any) -> int:
    if tree is None:
        return 0
    return sum(1 for node in tree.root_node.children if node.is_named)


def _extract_python_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Walk root_node children for Python import_statement and import_from nodes.

    Returns absolute module paths only (e.g. 'brain.state', 'shared.config').
    ``req`` is accepted for registry-uniform dispatch and unused here — Python
    imports are already absolute module paths and need no lexical resolution.
    """
    imports: list[str] = []
    for node in tree.root_node.children:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    text = child.text.decode("utf-8")
                    if text:
                        imports.append(text)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        text = name_node.text.decode("utf-8")
                        if text:
                            imports.append(text)
        elif node.type in ("import_from_statement", "import_from"):
            module_node = node.child_by_field_name("module_name")
            if module_node is None:
                continue
            text = module_node.text.decode("utf-8")
            # TODO(DEBT-087): relative imports ("from .mod import x") are skipped —
            # TS/JS now resolve relatives lexically, so Python module boundaries are
            # asymmetric in the dependency graph until resolution is added here too.
            if text and not text.startswith("."):
                imports.append(text)
    return imports


# JavaScript/TypeScript source extensions, tried when resolving an extensionless
# relative specifier and stripped from a specifier that carries one explicitly.
_JS_TS_EXTS: Tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _string_literal_text(string_node: Any) -> str:
    """Return the unquoted content of a tree-sitter ``string`` node.

    Prefers the ``string_fragment`` child (grammar-provided, quote-free); falls
    back to stripping the surrounding quote characters from the raw node text.
    """
    for child in string_node.children:
        if child.type == "string_fragment":
            return child.text.decode("utf-8")
    raw = string_node.text.decode("utf-8")
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return ""


def _resolve_relative_specifier(spec: str, req: IndexingRequest) -> Optional[str]:
    """Lexically resolve a relative TS/JS specifier to an extensionless workspace path.

    Pure string math — no filesystem access. Uses ``posixpath`` on forward-slashed
    input so a Windows-origin path (``C:\\ws\\a.ts``) resolves identically on a Linux
    or Alpine worker (where ``os.path`` treats ``\\`` as an ordinary filename char).
    A specifier that escapes ``workspace_root`` is dropped (returns ``None``).
    """
    base = posixpath.dirname(req.file_path.replace("\\", "/"))
    normalized = posixpath.normpath(posixpath.join(base, spec))
    for ext in _JS_TS_EXTS:
        if normalized.endswith(ext):
            normalized = normalized[: -len(ext)]
            break
    ws = req.workspace_root.replace("\\", "/")
    if ws:
        safe_root = ws.rstrip("/") + "/"
        if not (normalized + "/").startswith(safe_root):
            return None  # directory escape — drop edge
    return normalized


def _extract_ecmascript_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract module dependencies from a TypeScript/JavaScript AST.

    One walk serves all TS/JS variants (the grammars emit identical import node
    types). Captures static ``import``/re-export ``export … from``, dynamic
    ``import('…')``, and ``require('…')`` — the latter two nest arbitrarily, so
    the whole tree is walked, not just top-level nodes. Bare/package specifiers
    are emitted as-is (resolved to INFERRED downstream); relative specifiers are
    lexically resolved and workspace-confined. Template/computed specifiers are
    non-lexical and skipped. Order-preserving dedup keeps the edge list clean.
    """
    specs: List[str] = []
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None:
                text = _string_literal_text(source)
                if text:
                    specs.append(text)
        elif node_type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and (
                func.type == "import"
                or (func.type == "identifier" and func.text == b"require")
            ):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for child in args.children:
                        if child.type == "string":
                            text = _string_literal_text(child)
                            if text:
                                specs.append(text)
                            break
        # Push children reversed so the stack yields them in document (pre-order)
        # order — dependency edges preserve the source's import ordering.
        stack.extend(reversed(node.children))

    out: List[str] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.startswith("."):
            resolved = _resolve_relative_specifier(spec, req)
            if resolved is None:
                continue
            target = resolved
        else:
            target = spec
        if target not in seen:
            seen.add(target)
            out.append(target)
    return out


# Import-edge extractors keyed by VS Code languageId. Dispatch is O(1); an
# unregistered language yields no edges (best-effort, mirroring the worker's
# never-raise contract). Further languages are a single registry entry plus one
# extractor, added when a corpus exercises them — not speculatively.
IMPORT_EXTRACTORS: Dict[str, Callable[[Any, IndexingRequest], List[str]]] = {
    "python": _extract_python_imports,
    "typescript": _extract_ecmascript_imports,
    "typescriptreact": _extract_ecmascript_imports,
    "javascript": _extract_ecmascript_imports,
    "javascriptreact": _extract_ecmascript_imports,
}


def index_file_sync(req: IndexingRequest) -> IndexingResult:
    """Worker entry point: parse file AST, return a picklable result.

    Never raises — returns IndexingResult(success=False, error=...) on any exception
    so the asyncio caller always gets a result, never an unhandled worker exception.
    """
    global _worker_ast
    if _worker_ast is None:
        _worker_init()  # lazy fallback if pool was created without initializer
    ast_engine = _worker_ast
    if ast_engine is None:
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=0,
            language_id=req.language_id,
            success=False,
            error="AST engine unavailable",
        )
    try:
        tree = ast_engine.parse(
            req.file_path, req.content, req.language_id
        )
        imports: list[str] = []
        if tree is not None:
            extractor = IMPORT_EXTRACTORS.get(req.language_id)
            if extractor is not None:
                imports = extractor(tree, req)
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=_count_top_level_symbols(tree),
            language_id=req.language_id,
            success=True,
            imports=imports,
        )
    except Exception as exc:
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=0,
            language_id=req.language_id,
            success=False,
            error=str(exc),
        )


def calculate_ppr_sync(req: PPRRequest) -> PPRResult:
    """Compute node centrality over the project dependency graph.

    CPU-bound — runs in ProcessPoolExecutor. Returns a centrality score for every
    node. Phase 3.3 uses this as the Graph_Centrality term in CSS. Uses pure-Python
    degree centrality (no scipy) so the runtime stays free of native C/Fortran
    extensions for lightweight bundling.
    """
    if len(req.edges) > MAX_GRAPH_EDGES:
        logger.warning(
            "Dependency graph exceeds the edge cap (%d > %d) — skipping centrality.",
            len(req.edges), MAX_GRAPH_EDGES,
        )
        return PPRResult(scores={}, success=True)
    G: Any = None
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(req.edges)
        if len(G) == 0:
            return PPRResult(scores={}, success=True)
        scores: dict[str, float] = nx.degree_centrality(G)
        return PPRResult(scores=scores, success=True)
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))
    finally:
        if G is not None:
            G.clear()


def _candidate_paths(normalized_target: str) -> Iterator[str]:
    """Lazily yield indexed-file candidates for an extensionless relative target.

    A TS/JS relative specifier resolves to an extensionless workspace path; the
    concrete file it names may carry any JS/TS extension or be a directory's
    ``index.*`` barrel. Yielding lazily lets the caller short-circuit on the first
    membership hit — no per-edge candidate list is materialized.
    """
    yield normalized_target
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        yield normalized_target + ext
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        yield normalized_target + "/index" + ext


def resolve_target_to_file(
    target: str, indexed: set[str], norm_indexed: Dict[str, str]
) -> Optional[str]:
    """Map a stored ``target_dependency`` to a concrete indexed file, or None.

    Direct membership first, then extension/``index.*`` candidate expansion for an
    extensionless TS/JS specifier. Pure string math over the pre-built indexed set and
    its forward-slash lookup (``norm_indexed``); no filesystem access. Shared by
    confidence scoring and the blast-radius mapper so both resolve edges identically.
    """
    if target in indexed:
        return target
    normalized_target = target.replace("\\", "/")
    hit = next((c for c in _candidate_paths(normalized_target) if c in norm_indexed), None)
    return norm_indexed[hit] if hit is not None else None


def _resolve_edge_confidence(
    edges: Tuple[Tuple[str, str], ...], indexed_files: Tuple[str, ...]
) -> Tuple[Tuple[str, str, str, float], ...]:
    """Derive a confidence label/score per edge from whole-graph resolution.

    EXTRACTED (1.0): the target resolves to an indexed source file — directly, or
    (for an extensionless relative TS/JS specifier) via extension/``index.*``
    candidate expansion against the indexed set. AMBIGUOUS (0.25): the target's
    module stem matches ≥2 indexed files, so which file it refers to cannot be
    disambiguated. INFERRED (0.5): everything else — an external/unindexed module.

    All resolution is in-memory string math over the indexed set; no filesystem access.
    """
    indexed = set(indexed_files)
    norm_indexed = {f.replace("\\", "/"): f for f in indexed_files}
    stems: Counter[str] = Counter()
    for f in indexed_files:
        stem = os.path.splitext(os.path.basename(f.replace("\\", "/")))[0]
        if stem:
            stems[stem] += 1

    out: List[Tuple[str, str, str, float]] = []
    for source, target in edges:
        resolved = resolve_target_to_file(target, indexed, norm_indexed)
        if resolved is not None:
            out.append((source, resolved, "EXTRACTED", 1.0))
            continue
        module_stem = target.replace("\\", "/").rsplit("/", 1)[-1].split(".")[-1]
        if stems.get(module_stem, 0) >= 2:
            out.append((source, target, "AMBIGUOUS", 0.25))
        else:
            out.append((source, target, "INFERRED", 0.5))
    return tuple(out)


def calculate_graph_analytics_sync(req: PPRRequest) -> PPRResult:
    """Unified graph analytics over the project dependency graph (one DiGraph build).

    CPU-bound — runs in ProcessPoolExecutor. Computes degree centrality (pure-Python,
    no scipy), Louvain community detection (on the undirected projection, fixed seed
    for stable colors), and per-edge confidence. Supersedes calculate_ppr_sync on the
    batch path; the latter is retained for callers that only need scores.
    """
    if len(req.edges) > MAX_GRAPH_EDGES:
        logger.warning(
            "Dependency graph exceeds the edge cap (%d > %d) — skipping analytics.",
            len(req.edges), MAX_GRAPH_EDGES,
        )
        return PPRResult(scores={}, success=True)
    G: Any = None
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(req.edges)
        if len(G) == 0:
            return PPRResult(scores={}, success=True)

        # Pure-Python degree centrality (no scipy) — keeps the runtime free of
        # native C/Fortran extensions for lightweight bundling. Best-effort so a
        # centrality hiccup never sinks community detection or confidence.
        scores: dict[str, float] = {}
        try:
            scores = nx.degree_centrality(G)
        except Exception as exc:  # noqa: BLE001 — centrality is best-effort
            logger.warning("Degree centrality unavailable (non-fatal): %s", exc)

        # Louvain runs on the undirected projection, which transiently doubles
        # the graph in memory — bind it so it can be released deterministically
        # rather than waiting on GC in the reused pool worker.
        communities: Dict[str, int] = {}
        undirected: Any = None
        try:
            undirected = G.to_undirected()
            partition = nx.community.louvain_communities(undirected, seed=42)
            for idx, members in enumerate(partition):
                for node in members:
                    communities[node] = idx
        except Exception as exc:  # noqa: BLE001 — community detection is best-effort
            logger.warning("Louvain community detection failed (non-fatal): %s", exc)
        finally:
            if undirected is not None:
                undirected.clear()

        edge_confidence = _resolve_edge_confidence(req.edges, req.indexed_files)
        return PPRResult(
            scores=scores,
            success=True,
            communities=communities,
            edge_confidence=edge_confidence,
        )
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))
    finally:
        if G is not None:
            G.clear()


# ── Architecture-overview digest ──────────────────────────────────────────────
# Synthesizes the persisted graph analytics into one bounded orientation payload.
# Pure and picklable: it takes plain, already-relativized data and touches no I/O,
# so it stays inside this process-pool-safe module without pulling a DB dependency.

# Per-section output caps — the primary bound on digest size (token hygiene). A
# capped section reports its true ``total`` beside the truncated slice so a caller
# knows more exist.
_HOTSPOT_LIMIT: int = 20
_MODULE_LIMIT: int = 20
_CLUSTER_LIMIT: int = 15
_ENTRYPOINT_LIMIT: int = 25

# Basenames that mark a real application entrypoint. Deliberately excludes test
# files: a test module is not an architectural entrypoint. (The dead-code scanner
# uses a wider notion that counts tests — to avoid flagging them as orphans — which
# is the wrong semantics for an orientation digest.)
_ARCH_ENTRYPOINT_BASENAMES: frozenset[str] = frozenset(
    {"main.py", "__main__.py", "app.py", "manage.py", "cli.py", "wsgi.py", "asgi.py"}
)

_ROOT_MODULE_LABEL: str = "<root>"
_NO_EXTENSION_LABEL: str = "<none>"


def _empty_digest() -> Dict[str, object]:
    """A well-formed, zero-valued digest mirroring the populated shape exactly.

    Returned for both an empty/cold project and the tool's fail-open path so every
    consumer key (``digest["languages"]`` …) is always present with its correct
    type — an empty ``{}`` would raise ``KeyError`` in a downstream reader or gate.
    """
    return {
        "languages": {"total": 0, "top": []},
        "top_modules": {"total": 0, "top": []},
        "hotspots": {"total": 0, "top": []},
        "community_clusters": {"count": 0, "largest": []},
        "entrypoints": {"total": 0, "top": []},
        "graph_schema": {"indexed_files": 0, "edges": 0},
    }


def build_architecture_digest_sync(
    *,
    rel_files: Tuple[str, ...],
    top_ppr_rel: Tuple[Tuple[str, float], ...],
    community_ids: Tuple[int, ...],
    edge_count: int,
) -> Dict[str, object]:
    """Assemble a bounded, deterministic architecture-overview digest.

    All inputs are plain, workspace-relative, forward-slash data (relativized by the
    caller). Deterministic: every list is sorted by a total order and truncated to a
    module-constant cap, and each capped section carries its true ``total``.
    """
    if not rel_files and not top_ppr_rel and not community_ids:
        return _empty_digest()

    # Languages by file extension (deterministic: -count, then extension label).
    lang_counter: Counter[str] = Counter()
    for f in rel_files:
        _, ext = os.path.splitext(f)
        lang_counter[ext.lower() if ext else _NO_EXTENSION_LABEL] += 1
    lang_sorted = sorted(lang_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    languages = {
        "total": len(lang_sorted),
        "top": [{"language": ext, "count": n} for ext, n in lang_sorted[:_MODULE_LIMIT]],
    }

    # Top-level modules (first path segment; root files fold into one label).
    mod_counter: Counter[str] = Counter()
    for f in rel_files:
        mod_counter[f.split("/", 1)[0] if "/" in f else _ROOT_MODULE_LABEL] += 1
    mod_sorted = sorted(mod_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    top_modules = {
        "total": len(mod_sorted),
        "top": [{"module": m, "count": n} for m, n in mod_sorted[:_MODULE_LIMIT]],
    }

    # Hotspots — highest-centrality files (stable secondary sort by path).
    hot_sorted = sorted(top_ppr_rel, key=lambda ps: (-ps[1], ps[0]))
    hotspots = {
        "total": len(hot_sorted),
        "top": [{"file": p, "score": s} for p, s in hot_sorted[:_HOTSPOT_LIMIT]],
    }

    # Community clusters — sizes per Louvain id (largest first, id tie-break).
    cluster_counter: Counter[int] = Counter(community_ids)
    cluster_sorted = sorted(cluster_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    community_clusters = {
        "count": len(cluster_sorted),
        "largest": [{"id": cid, "size": n} for cid, n in cluster_sorted[:_CLUSTER_LIMIT]],
    }

    # Entrypoints — files whose basename marks an application entry (no test files).
    entry_files = sorted(
        f for f in rel_files if f.rsplit("/", 1)[-1] in _ARCH_ENTRYPOINT_BASENAMES
    )
    entrypoints = {
        "total": len(entry_files),
        "top": entry_files[:_ENTRYPOINT_LIMIT],
    }

    return {
        "languages": languages,
        "top_modules": top_modules,
        "hotspots": hotspots,
        "community_clusters": community_clusters,
        "entrypoints": entrypoints,
        "graph_schema": {"indexed_files": len(rel_files), "edges": edge_count},
    }
