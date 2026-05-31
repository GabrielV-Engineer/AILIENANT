"""api/memory_dashboard.py — Phase 7.9.B.1.

Read-only REST surface for the Web Dashboard's Memory Management panel.

The dashboard is a standalone browser SPA served by FastAPI at /dashboard. It
cannot receive the extension host's BroadcastChannel pushes, so it pulls memory
data over same-origin REST. To avoid loading every project's memory at once (a
crash hazard), the UI first lists indexed *sections* (top-level folders) and
only fetches a section's graph/vectors when the user clicks it.

Endpoints (prefix /api/v1/memory):
  GET /sections  — enumerate indexed folders per project (cheap; no vectors).
  GET /graph     — code dependency graph for one section (SQLite only).
  GET /vectors   — 2D PCA projection of a section's embeddings (LanceDB + numpy).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core import db as catalog_db
from core.memory.semantic_memory import SemanticMemoryManager, pca_project_2d

router = APIRouter(prefix="/api/v1/memory", tags=["memory-dashboard"])

# Same allowlist used by the vector store — defense-in-depth on project_id.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


# =====================================================================
# Response models
# =====================================================================


class SectionInfo(BaseModel):
    project_id: str
    folder: str        # human label, relative to the project's common root
    abs_prefix: str    # normalized absolute prefix the graph/vectors endpoints match on
    file_count: int
    has_vectors: bool  # heuristic (file_count > 0); real state discovered lazily


class SectionsResponse(BaseModel):
    sections: List[SectionInfo]
    project_count: int


class GraphNode(BaseModel):
    id: str
    label: str
    ppr_score: float
    in_degree: int
    out_degree: int
    is_external: bool   # True when the node is an unresolved module name, not a source file
    full_path: str
    leiden_community_id: Optional[int] = None   # Louvain community for coloring; None until computed
    is_god_node: bool = False                   # top-3 by degree centrality — rendered larger


class GraphEdge(BaseModel):
    source: str
    target: str
    confidence: Optional[str] = None            # EXTRACTED | INFERRED | AMBIGUOUS (None → treat solid)
    confidence_score: Optional[float] = None


class GraphResponse(BaseModel):
    project_id: str
    folder: str
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    total_nodes: int    # pre-cap node count
    capped: bool


class VectorPoint(BaseModel):
    file_path: str
    label: str
    x: float
    y: float
    token_count: int
    snippet: str


class VectorMapResponse(BaseModel):
    project_id: str
    folder: str
    points: List[VectorPoint]
    point_count: int
    degenerate: bool
    variance_explained: List[float]


# =====================================================================
# Helpers
# =====================================================================


def _norm(path: str) -> str:
    """Normalize separators to forward slashes for cross-platform prefix matching."""
    return path.replace("\\", "/")


def _rank_god_nodes(
    candidates: List[str],
    in_deg: Dict[str, int],
    out_deg: Dict[str, int],
    ppr: Dict[str, float],
    top_n: int = 3,
) -> set[str]:
    """Top-N nodes by degree centrality (in+out), deterministic tiebreak by PPR then id.

    "God Nodes" are the structural hubs of the dependency graph; rendered larger
    in the viewer. Degree is the centrality of record here (cheap, already on hand);
    PPR breaks ties so the ranking is stable across renders.
    """
    ranked = sorted(
        candidates,
        key=lambda n: (-(in_deg.get(n, 0) + out_deg.get(n, 0)), -ppr.get(n, 0.0), n),
    )
    return set(ranked[:top_n])


def _common_root(paths: List[str]) -> str:
    """Longest common directory prefix for a project's files (normalized).

    Falls back to the empty string when paths span multiple drives or there is a
    single file — callers then treat the file's own dirname as the root.
    """
    if not paths:
        return ""
    try:
        root = os.path.commonpath(paths)
        return _norm(root)
    except ValueError:
        return ""


# =====================================================================
# Endpoints
# =====================================================================


@router.get("/sections", response_model=SectionsResponse)
async def get_sections(depth: int = Query(1, ge=1, le=3)) -> SectionsResponse:
    """Enumerate indexed sections (top-level folders) per project.

    Derived purely from the indexed_files table — no vectors or PPR scores are
    loaded here, keeping this O(rows) and cheap regardless of memory size.
    """
    rows = await catalog_db.get_all_indexed_files()

    by_project: Dict[str, List[str]] = defaultdict(list)
    for file_path, project_id in rows:
        by_project[project_id].append(_norm(file_path))

    sections: List[SectionInfo] = []
    for project_id, files in by_project.items():
        root = _common_root(files)
        # (section_label, abs_prefix) → file_count
        counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for f in files:
            if root and f.startswith(root):
                remainder = f[len(root):].lstrip("/")
            else:
                remainder = os.path.basename(f)
            segments = [s for s in remainder.split("/") if s]
            # Drop the trailing filename so sections are folders, not files.
            dir_segments = segments[:-1] if segments else []
            if dir_segments:
                label = "/".join(dir_segments[:depth])
            else:
                label = "(root)"
            base = root if root else _norm(os.path.dirname(f))
            abs_prefix = f"{base}/{label}" if label != "(root)" else base
            counts[(label, abs_prefix)] += 1

        for (label, abs_prefix), count in sorted(counts.items()):
            sections.append(SectionInfo(
                project_id=project_id,
                folder=label,
                abs_prefix=abs_prefix,
                file_count=count,
                has_vectors=count > 0,
            ))

    return SectionsResponse(sections=sections, project_count=len(by_project))


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    project_id: str = Query(...),
    folder: str = Query(""),
    max_nodes: int = Query(300, ge=10, le=2000),
) -> GraphResponse:
    """Code dependency graph for one section. SQLite only — no vector store hit.

    Nodes are source files (filterable by the section's folder prefix) plus the
    Python modules they import (flagged is_external). Sized/colored by PageRank
    downstream. Capped to the top max_nodes by PPR to bound the payload.
    """
    if not _SAFE_ID_RE.match(project_id):
        raise HTTPException(status_code=400, detail="invalid project_id")

    edges_raw = await catalog_db.get_graph_edges_enriched(project_id)
    folder_prefix = _norm(folder)

    kept: List[Tuple[str, str, Optional[str], Optional[float]]] = []
    for source, target, conf, conf_score in edges_raw:
        if not folder_prefix or _norm(source).startswith(folder_prefix):
            kept.append((source, target, conf, conf_score))

    source_set = {s for s, _, _, _ in kept}
    in_deg: Dict[str, int] = defaultdict(int)
    out_deg: Dict[str, int] = defaultdict(int)
    node_ids = set()
    for source, target, _, _ in kept:
        out_deg[source] += 1
        in_deg[target] += 1
        node_ids.add(source)
        node_ids.add(target)

    ppr = await catalog_db.get_ppr_scores_bulk(list(node_ids), project_id)
    communities = await catalog_db.get_community_ids_bulk(list(node_ids), project_id)

    total_nodes = len(node_ids)
    # Cap: keep the top max_nodes by PPR (deterministic tiebreak by id).
    ordered = sorted(node_ids, key=lambda n: (-ppr.get(n, 0.0), n))
    capped = total_nodes > max_nodes
    keep_ids = set(ordered[:max_nodes]) if capped else node_ids

    # God Nodes: top-3 internal nodes by degree centrality (in+out), tiebreak PPR.
    internal_kept = [n for n in keep_ids if n in source_set]
    god_nodes = _rank_god_nodes(internal_kept, in_deg, out_deg, ppr)

    nodes: List[GraphNode] = []
    for nid in sorted(keep_ids):
        is_external = nid not in source_set
        nodes.append(GraphNode(
            id=nid,
            label=os.path.basename(_norm(nid)) if not is_external else nid,
            ppr_score=ppr.get(nid, 0.0),
            in_degree=in_deg.get(nid, 0),
            out_degree=out_deg.get(nid, 0),
            is_external=is_external,
            full_path=nid,
            leiden_community_id=communities.get(nid),
            is_god_node=nid in god_nodes,
        ))

    edges = [
        GraphEdge(source=s, target=t, confidence=conf, confidence_score=conf_score)
        for s, t, conf, conf_score in kept
        if s in keep_ids and t in keep_ids
    ]

    return GraphResponse(
        project_id=project_id,
        folder=folder,
        nodes=nodes,
        edges=edges,
        total_nodes=total_nodes,
        capped=capped,
    )


@router.get("/vectors", response_model=VectorMapResponse)
async def get_vectors(
    project_id: str = Query(...),
    folder: str = Query(""),
    max_points: int = Query(2000, ge=10, le=10000),
) -> VectorMapResponse:
    """2D PCA projection of a section's embeddings for the semantic scatter map."""
    if not _SAFE_ID_RE.match(project_id):
        raise HTTPException(status_code=400, detail="invalid project_id")

    sem = SemanticMemoryManager()
    rows = await sem.dump_vectors(project_id, folder_prefix=folder, max_rows=max_points)

    if not rows:
        return VectorMapResponse(
            project_id=project_id, folder=folder, points=[],
            point_count=0, degenerate=False, variance_explained=[0.0, 0.0],
        )

    # Deterministic ordering so the projection is stable across requests.
    rows.sort(key=lambda r: str(r.get("file_path", "")))
    vectors = [list(r.get("vector") or []) for r in rows]

    coords, var_exp, degenerate = pca_project_2d(vectors)

    points: List[VectorPoint] = []
    for r, (x, y) in zip(rows, coords):
        fp = str(r.get("file_path", ""))
        points.append(VectorPoint(
            file_path=fp,
            label=os.path.basename(_norm(fp)),
            x=float(x),
            y=float(y),
            token_count=int(r.get("token_count") or 0),
            snippet=str(r.get("content_snippet") or ""),
        ))

    return VectorMapResponse(
        project_id=project_id,
        folder=folder,
        points=points,
        point_count=len(points),
        degenerate=degenerate,
        variance_explained=var_exp,
    )
