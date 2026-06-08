"""
brain/memory.py — Process-pool-safe indexing bridge.

All functions are module-level so ProcessPoolExecutor can pickle them.
Phase 3 extends this module with LanceDB vector indexing and GraphRAG topology extraction.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from shared.contracts import IndexingRequest, IndexingResult, PPRRequest, PPRResult

logger = logging.getLogger("MEMORY_WORKER")

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


def _extract_python_imports(tree: Any) -> list[str]:
    """Walk root_node children for Python import_statement and import_from nodes.

    Returns absolute module paths only (e.g. 'brain.state', 'shared.config').
    Relative imports (from . import X) are silently skipped — resolving them
    to absolute paths requires project-root context not available in the worker.
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
            if text and not text.startswith("."):  # skip relative imports
                imports.append(text)
    return imports


def index_file_sync(req: IndexingRequest) -> IndexingResult:
    """Worker entry point: parse file AST, return a picklable result.

    Never raises — returns IndexingResult(success=False, error=...) on any exception
    so the asyncio caller always gets a result, never an unhandled worker exception.
    """
    global _worker_ast
    if _worker_ast is None:
        _worker_init()  # lazy fallback if pool was created without initializer
    try:
        tree = _worker_ast.parse(  # type: ignore[union-attr]
            req.file_path, req.content, req.language_id
        )
        imports: list[str] = []
        if tree is not None and req.language_id == "python":
            imports = _extract_python_imports(tree)
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
    try:
        import networkx as nx
        G: Any = nx.DiGraph()
        G.add_edges_from(req.edges)
        if len(G) == 0:
            return PPRResult(scores={}, success=True)
        scores: dict[str, float] = nx.degree_centrality(G)
        return PPRResult(scores=scores, success=True)
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))


def _resolve_edge_confidence(
    edges: Tuple[Tuple[str, str], ...], indexed_files: Tuple[str, ...]
) -> Tuple[Tuple[str, str, str, float], ...]:
    """Derive a confidence label/score per edge from whole-graph resolution.

    EXTRACTED (1.0): the target is itself an indexed source file — a concrete,
    parsed internal dependency. AMBIGUOUS (0.25): the target's module stem matches
    ≥2 indexed files, so which file it refers to cannot be disambiguated. INFERRED
    (0.5): everything else — an external/unindexed module the import implies.
    """
    indexed = set(indexed_files)
    stems: Counter[str] = Counter()
    for f in indexed_files:
        stem = os.path.splitext(os.path.basename(f.replace("\\", "/")))[0]
        if stem:
            stems[stem] += 1

    out: List[Tuple[str, str, str, float]] = []
    for source, target in edges:
        if target in indexed:
            out.append((source, target, "EXTRACTED", 1.0))
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
    try:
        import networkx as nx
        G: Any = nx.DiGraph()
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

        communities: Dict[str, int] = {}
        try:
            partition = nx.community.louvain_communities(G.to_undirected(), seed=42)
            for idx, members in enumerate(partition):
                for node in members:
                    communities[node] = idx
        except Exception as exc:  # noqa: BLE001 — community detection is best-effort
            logger.warning("Louvain community detection failed (non-fatal): %s", exc)

        edge_confidence = _resolve_edge_confidence(req.edges, req.indexed_files)
        return PPRResult(
            scores=scores,
            success=True,
            communities=communities,
            edge_confidence=edge_confidence,
        )
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))
