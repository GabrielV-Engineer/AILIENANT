"""core/boundary_graph.py — cross-boundary link edges (WS / MCP inter-process seams).

The file-level ``dependency_graph`` models "who imports whom", but the extension (TS)
and core (Python) never import across the process boundary — they communicate only
through a string-keyed WS message union (``api/ws_contracts.py``) and an MCP tool
catalog (``gateway/catalog.py``). This module resolves that contract into a SEPARATE,
namespaced edge layer so the graph can answer "what handles ``server_stream_end``"
across the boundary, WITHOUT ever touching the code-dependency traversal
(``_bfs_k_hop`` / blast-radius / PPR / dead-code) — non-pollution is structural: the
edges live in their own ``boundary_edges`` table.

Fidelity, by construction of the seam:
  * **declares** — the channel's declaration site (the ``Literal`` in the WS union, the
    ``Capability(name=…)`` in the MCP catalog). High-precision.
  * **handles** — a real dispatch on the channel literal (the frontend ``case`` switch,
    the MCP ``{name: handler}`` table). High-precision; this is the primary query.
  * **emits** — best-effort: only recoverable where a literal *send-site* exists
    (extension ``client_*`` object sends). A backend ``server_*`` emit is a typed model
    construction (``ServerStreamEndEvent(data=…)``) with no channel literal at the send
    site, so those emit edges are NOT recoverable by literal analysis (a tracked gap).
  * **references** — the honest bucket for a channel a file mentions but whose role is
    undetermined (non-``server_``/``client_`` prefix, or an unknown boundary side).

Resolution reuses the FTS5 trigram narrowing (subprocess-free, catalog-scoped) then
confirms each candidate by matching the channel as a QUOTED string literal — channels
are always referenced quoted at real sites, so quote-anchoring rejects substring
false-positives (``run_task`` vs ``rerun_task``) and prose/backtick docstring mentions
alike. The whole graph is a cheap full rebuild (~71 channels), so it is always
consistent with the current catalog with no per-file incremental coupling. Output is
advisory / READ_ONLY: an empty ``handlers`` list means "no handler found via this
resolution path", never "no handler exists".
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from core.db import (
    fts_narrow_catalog,
    get_boundary_edges,
    list_indexed_files,
    replace_boundary_edges,
)

logger = logging.getLogger(__name__)

_READ_MAX_BYTES: int = 100_000  # mirrors the analyst/symbol-refs disk cap (token + RAM hygiene)

# The two boundary sides are the workspace's top-level subdirectories.
_EXTENSION_MARKER = "ailienant-extension"
_CORE_MARKER = "ailienant-core"

# Default declaration-file suffixes (overridable via BoundaryRegistry for tests).
_WS_DECL_SUFFIX = "api/ws_contracts.py"
_MCP_DECL_SUFFIX = "gateway/catalog.py"


@dataclass(frozen=True)
class BoundaryRegistry:
    """The channel node-set of AILIENANT's inter-process contract.

    ``ws_channels`` are the ``event_type`` discriminator literals; ``mcp_channels`` are
    the MCP verb names. The ``*_decl_suffix`` fields identify the declaration files in
    the indexed catalog (matched by path suffix, so an edge points at the *indexed*
    file regardless of the backend's install path). Injectable so tests can seed a
    synthetic contract without importing the real modules.
    """
    ws_channels: FrozenSet[str]
    mcp_channels: FrozenSet[str]
    ws_decl_suffix: str = _WS_DECL_SUFFIX
    mcp_decl_suffix: str = _MCP_DECL_SUFFIX


@dataclass
class BoundaryTrace:
    """Advisory trace for one channel. Empty ``handlers`` never means "no handler exists"."""
    channel: str
    seam: str = ""
    declared_in: List[str] = field(default_factory=list)
    handlers: List[str] = field(default_factory=list)
    emitters: List[str] = field(default_factory=list)
    references: List[str] = field(default_factory=list)
    in_catalog: bool = False


def _introspect_registry() -> BoundaryRegistry:
    """Build the registry from AILIENANT's own contract via lazy runtime introspection.

    Imports ``api.ws_contracts`` / ``gateway.catalog`` lazily (never at module import)
    so ``core/`` stays decoupled from the ``api/`` and ``gateway/`` domains and dodges
    their transitive import cost until a boundary trace is actually requested.
    """
    import api.ws_contracts as ws_contracts  # deferred: keep core/ decoupled from api/
    from gateway.catalog import capability_names  # deferred: keep core/ decoupled from gateway/

    ws: Set[str] = set()
    for obj in vars(ws_contracts).values():
        model_fields = getattr(obj, "model_fields", None)
        if not isinstance(model_fields, dict) or "event_type" not in model_fields:
            continue
        default = getattr(model_fields["event_type"], "default", None)
        if isinstance(default, str) and default:
            ws.add(default)
    return BoundaryRegistry(ws_channels=frozenset(ws), mcp_channels=frozenset(capability_names()))


def _side(file_path: str) -> str:
    """Which side of the boundary a file lives on: 'extension' | 'core' | '' (unknown)."""
    norm = file_path.replace("\\", "/")
    if _EXTENSION_MARKER in norm:
        return "extension"
    if _CORE_MARKER in norm:
        return "core"
    return ""


def _classify(seam: str, channel: str, file_path: str, decl_files: FrozenSet[str]) -> str:
    """Map a confirmed (file, channel) reference to an edge kind.

    Direction is deterministic: (boundary side) × (channel prefix). A backend
    ``server_*`` literal is a mention (the real emit is typed), so it is ``references``,
    never a fabricated ``emits``.
    """
    if file_path.replace("\\", "/") in decl_files:
        return "declares"
    side = _side(file_path)
    if seam == "mcp":
        return "handles" if "/gateway/" in file_path.replace("\\", "/") else "references"
    # WS seam.
    if channel.startswith("server_"):
        return "handles" if side == "extension" else "references"
    if channel.startswith("client_"):
        if side == "core":
            return "handles"
        if side == "extension":
            return "emits"
    return "references"


def _read_capped(path: str, project_id: str, workspace_root: str) -> Optional[str]:
    """Freshest bytes (RAM-VFS ∪ disk) for a candidate, workspace-jailed and byte-capped."""
    from core.vfs_middleware import VFSMiddleware  # deferred — avoid import cycle
    try:
        res = VFSMiddleware().read_safe(path, project_id=project_id, project_root=workspace_root)
    except Exception as exc:  # noqa: BLE001 — a single unreadable candidate must not abort the rebuild
        logger.debug("boundary_graph: VFS read error for %s: %s", path, exc)
        return None
    if not res.ok or res.content is None:
        return None
    return res.content[:_READ_MAX_BYTES]


# In-flight rebuild futures, keyed by (event-loop id, project_id): concurrent callers
# share one rebuild (single-flight) rather than each re-scanning the catalog. The
# get/set of a key never straddles an await, so it is atomic within the event loop.
_inflight: Dict[Tuple[int, str], "asyncio.Future[int]"] = {}


async def refresh_boundary_graph(
    project_id: str,
    workspace_root: str = "",
    *,
    registry: Optional[BoundaryRegistry] = None,
) -> int:
    """Full-rebuild the boundary graph from the current catalog; return the edge count.

    Single-flight per project: a concurrent call awaits the in-flight rebuild instead
    of launching a second scan. The multi-second file scan runs OUTSIDE any graph lock;
    only the final atomic replace (in ``replace_boundary_edges``) takes the project lock.
    """
    loop = asyncio.get_running_loop()
    key = (id(loop), project_id)
    existing = _inflight.get(key)
    if existing is not None:
        return await existing
    fut: "asyncio.Future[int]" = loop.create_future()
    _inflight[key] = fut
    try:
        count = await _do_refresh(project_id, workspace_root, registry)
        fut.set_result(count)
        return count
    except Exception as exc:
        fut.set_exception(exc)
        raise
    finally:
        _inflight.pop(key, None)


async def _do_refresh(
    project_id: str, workspace_root: str, registry: Optional[BoundaryRegistry]
) -> int:
    reg = registry if registry is not None else _introspect_registry()
    catalog = await list_indexed_files(project_id)
    norm_catalog = {p.replace("\\", "/") for p in catalog}

    decl_files: Set[str] = set()
    for p in norm_catalog:
        if p.endswith(reg.ws_decl_suffix) or p.endswith(reg.mcp_decl_suffix):
            decl_files.add(p)

    # channel -> seam, and per-channel compiled quoted-literal confirm regex.
    seam_of: Dict[str, str] = {}
    for ch in reg.ws_channels:
        seam_of[ch] = "ws"
    for ch in reg.mcp_channels:
        seam_of.setdefault(ch, "mcp")
    confirm_re: Dict[str, "re.Pattern[str]"] = {
        ch: re.compile("[\"']" + re.escape(ch) + "[\"']") for ch in seam_of
    }

    # Pass 1 — narrow each channel over the indexed catalog (superset; None → full scan),
    # then union the candidate files so each is read exactly once.
    candidate_files: Set[str] = set()
    for ch in seam_of:
        narrowed = await fts_narrow_catalog(project_id, ch, catalog)
        candidate_files.update(narrowed if narrowed is not None else catalog)

    # Pass 2 — read each candidate once, confirm every channel as a QUOTED literal, classify.
    edges: Set[Tuple[str, str, str, str]] = set()
    decl_frozen = frozenset(decl_files)
    for path in candidate_files:
        content = await asyncio.to_thread(_read_capped, path, project_id, workspace_root)
        if not content:
            continue
        for ch, pattern in confirm_re.items():
            if pattern.search(content):
                kind = _classify(seam_of[ch], ch, path, decl_frozen)
                edges.add((path.replace("\\", "/"), ch, kind, seam_of[ch]))

    edge_list = sorted(edges)
    await replace_boundary_edges(project_id, edge_list)
    logger.debug(
        "boundary_graph: rebuilt %d edges over %d channels for project %s",
        len(edge_list), len(seam_of), project_id or "<default>",
    )
    return len(edge_list)


async def trace_boundary(channel: str, project_id: str = "") -> BoundaryTrace:
    """Group the stored boundary edges for one channel into an advisory trace.

    Reads only ``boundary_edges`` — never the code-dependency graph. An empty
    ``handlers`` list means no handler was resolved via static reference analysis, NOT
    that the channel is unhandled.
    """
    rows = await get_boundary_edges(project_id, channel)
    trace = BoundaryTrace(channel=channel, in_catalog=bool(rows))
    for source_file, _ch, kind, seam in rows:
        trace.seam = trace.seam or seam
        if kind == "declares":
            trace.declared_in.append(source_file)
        elif kind == "handles":
            trace.handlers.append(source_file)
        elif kind == "emits":
            trace.emitters.append(source_file)
        else:
            trace.references.append(source_file)
    for bucket in (trace.declared_in, trace.handlers, trace.emitters, trace.references):
        bucket.sort()
    return trace
