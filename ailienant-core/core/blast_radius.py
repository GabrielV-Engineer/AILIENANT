"""Blast-radius mapper — transitive dependents of a changed file set.

A pre-apply safety validator: before a generated diff is written, it answers
"how many files transitively import what we're about to change?" over the
``dependency_graph`` and lets the caller escalate to human review when that count
crosses a threshold — the one automated backstop for a small edit that silently
breaks many downstream files under auto-apply.

Traversal is a resolved, in-memory reverse-adjacency BFS. The graph stores an edge
target as an import specifier (an extensionless workspace path for TS/JS, a dotted
module for Python), not the absolute file path a changed file is keyed by, so a raw
``target_dependency`` walk would miss dependents entirely. Every target is first
resolved back to a concrete indexed file — sharing the confidence resolver for
TS/JS and a fail-safe suffix match for Python — and the walk runs over that resolved
adjacency. The traversal mirrors the ``_bfs_k_hop`` pattern (visited set, per-hop
frontier, depth-bounded loop) and stays pure so it runs off the event loop.
"""
from __future__ import annotations

import asyncio
import posixpath
from typing import Dict, List, Set, Tuple

from brain.memory import resolve_target_to_file
from core import db as catalog_db

# Default reverse-traversal depth. Three hops captures direct importers and two
# further layers of transitive dependents — deep enough to surface a real ripple,
# shallow enough to stay cheap on a large graph.
DEFAULT_DEPTH: int = 3

# Advisory ceiling on the edge count a single blast-radius call will traverse.
# Above it the check is skipped (returns empty) rather than stalling the apply
# path on a pathologically large graph — the mapper is a backstop, not a gate that
# may block indefinitely. Comfortably above the 5K-node / 15K-edge stress target.
MAX_BLAST_EDGES: int = 20000


def _build_python_suffix_index(indexed_files: Tuple[str, ...]) -> Dict[str, List[str]]:
    """Map every segment-aligned path suffix of each indexed ``.py`` file to that file.

    A Python edge target is a dotted module (``brain.state``) with no lexically
    recoverable absolute path — the import root is a ``sys.path`` entry the worker
    never sees. Indexing each file by its path suffixes (``state``, ``brain/state``,
    ``pkg/brain/state``) lets ``brain.state`` → ``brain/state`` resolve in O(1). A
    suffix that matches several files maps to all of them: over-matching over-counts
    the radius, the safe direction for a review gate.
    """
    idx: Dict[str, List[str]] = {}
    for f in indexed_files:
        nf = f.replace("\\", "/")
        if not nf.endswith(".py"):
            continue
        stem = nf[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        parts = stem.split("/")
        for i in range(len(parts)):
            idx.setdefault("/".join(parts[i:]), []).append(nf)
    return idx


def _resolved_target_files(
    target: str,
    indexed: Set[str],
    norm_indexed: Dict[str, str],
    py_index: Dict[str, List[str]],
) -> List[str]:
    """Resolve one edge target to the concrete indexed file(s) it names."""
    direct = resolve_target_to_file(target, indexed, norm_indexed)
    if direct is not None:
        return [direct.replace("\\", "/")]
    # Python module (bare package name, e.g. "brain", or dotted, e.g. "brain.state"):
    # fail-safe suffix match (over-count, never under-count). Precise sys.path-aware
    # resolution is deferred. A slash/backslash rules out a TS/JS specifier, which
    # never reaches this branch via a bare single segment without one.
    if "/" not in target and "\\" not in target:
        return py_index.get(target.replace(".", "/"), [])
    return []


def _build_reverse_adjacency(
    edges: Tuple[Tuple[str, str], ...], indexed_files: Tuple[str, ...]
) -> Dict[str, Set[str]]:
    """Build ``resolved_target_file -> {source_file, ...}`` — pure, in-memory, no disk."""
    indexed: Set[str] = set(indexed_files)
    norm_indexed: Dict[str, str] = {f.replace("\\", "/"): f for f in indexed_files}
    py_index = _build_python_suffix_index(indexed_files)
    rev: Dict[str, Set[str]] = {}
    for source, target in edges:
        for resolved in _resolved_target_files(target, indexed, norm_indexed, py_index):
            rev.setdefault(resolved, set()).add(source.replace("\\", "/"))
    return rev


def _seed_forms(seed: str, workspace_root: str) -> Set[str]:
    """Yield the reverse-adjacency key form(s) a changed-file seed may match.

    OS-agnostic: forward-slash then ``posixpath`` (consistent with the extractor),
    never raw concatenation. A relative diff path is joined to the workspace; an
    already-absolute (posix or Windows-drive) seed is passed through unchanged. Both
    forms are offered so a relative path matches an absolute adjacency key regardless
    of which form the diff used — an unmatched form simply contributes no dependents.
    """
    ns = seed.replace("\\", "/")
    forms: Set[str] = {ns}
    if workspace_root and not ns.startswith("/") and not (len(ns) > 1 and ns[1] == ":"):
        forms.add(posixpath.normpath(posixpath.join(workspace_root.replace("\\", "/"), ns)))
    return forms


def compute_blast_radius_sync(
    seeds: Tuple[str, ...],
    edges: Tuple[Tuple[str, str], ...],
    indexed_files: Tuple[str, ...],
    depth: int = DEFAULT_DEPTH,
    workspace_root: str = "",
) -> List[str]:
    """Return the transitive dependents of ``seeds`` up to ``depth`` hops.

    Cycle-safe (a visited set seeded with the inputs), deterministic (sorted
    expansion over a list-ordered frontier), and pure so it runs off the event loop.
    Mirrors the ``_bfs_k_hop`` visited/frontier/depth structure. Seeds are excluded
    from the result — they are already changing.
    """
    if len(edges) > MAX_BLAST_EDGES:
        return []
    rev = _build_reverse_adjacency(edges, indexed_files)
    visited: Set[str] = set()
    frontier: List[str] = []
    for seed in seeds:
        # Defensive: a non-str seed can never silently AttributeError into the
        # caller's fail-open handler and disable the gate.
        if not isinstance(seed, str):
            continue
        for form in _seed_forms(seed, workspace_root):
            if form not in visited:
                visited.add(form)
                frontier.append(form)

    result: List[str] = []
    for _ in range(max(0, depth)):
        if not frontier:
            break
        next_frontier: List[str] = []
        for node in frontier:
            for dependent in sorted(rev.get(node, set())):
                if dependent not in visited:
                    visited.add(dependent)
                    next_frontier.append(dependent)
                    result.append(dependent)
        frontier = next_frontier
    return result


async def compute_blast_radius(
    project_id: str,
    seeds: List[str],
    depth: int = DEFAULT_DEPTH,
    workspace_root: str = "",
) -> List[str]:
    """Fetch the project graph and compute the blast radius off the event loop.

    The edge/node universe is read on the loop (async SQLite) and the pure traversal
    runs via ``asyncio.to_thread`` — the data is already in memory, so this avoids
    pickling the edge set to a subprocess while keeping the loop unblocked.
    """
    edges = await catalog_db.get_all_edges(project_id)
    indexed = await catalog_db.list_indexed_files(project_id)
    return await asyncio.to_thread(
        compute_blast_radius_sync,
        tuple(seeds),
        tuple(edges),
        tuple(indexed),
        depth,
        workspace_root,
    )
