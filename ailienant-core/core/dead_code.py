"""Dead-code detection — zero-resolved-in-degree, non-entrypoint file candidates.

A file-level orphan scan over ``dependency_graph``: an in-repo file that no other
in-repo file's import resolves to, and that isn't a known entrypoint (a FastAPI
route file, a pytest file, ``main.py``, a tool-registration call site) or
user-allowlisted via ``.ailienant/dead-code-allowlist.json``, is surfaced as a
dead-code candidate.

The graph is file-level only (no symbol/call-level substrate exists), so this is
an interim, coarse-grained signal, not a precise "this function is unused"
answer — a candidate still requires human review before deletion.

In-degree here is *resolved*, not the raw edge-target string used elsewhere for
graph visualization: a Python edge target is a dotted module (``brain.state``)
with no lexically recoverable absolute path, so treating the raw string as the
node key would make every file imported only via dotted-module syntax a false
orphan. Resolution reuses ``core.blast_radius``'s already-validated resolver.

Every file content read happens inside the caller's ``asyncio.to_thread`` dispatch,
and only for a file that already passed the zero-I/O gates (in-degree, filename,
allowlist) — most indexed files never have their content opened at all.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import pathlib
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

import pathspec

from core import db as catalog_db
from core.blast_radius import _build_python_suffix_index, _resolved_target_files
from core.vfs_middleware import make_safe_reader

logger = logging.getLogger("DEAD_CODE")

# ── Allowlist config ──────────────────────────────────────────────────────────
_ALLOWLIST_REL_PATH: str = ".ailienant/dead-code-allowlist.json"
_ALLOWLIST_MAX_PATTERNS: int = 500  # defensive cap — token/perf hygiene, not a product limit

# ── Content read bound (mirrors tools/analyst_tools.py's _DISK_MAX_BYTES) ────
# A full-content cap, not a fixed-byte truncation: truncating risks missing a
# decorator or __main__ guard that appears later in a legitimately large file,
# which would misclassify a live entrypoint as dead code (the wrong-direction
# failure for this feature). A file over the cap falls back to filename-only
# entrypoint detection — the same accepted edge case ComplexityAnalysisTool
# already lives with for its own 100 KB cap.
_CONTENT_MAX_BYTES: int = 100 * 1024

# ── Hardcoded entrypoint signals ──────────────────────────────────────────────
_TEST_FILENAME_PATTERNS: Tuple[str, ...] = ("test_*.py", "*_test.py", "conftest.py")
_ENTRYPOINT_BASENAMES: FrozenSet[str] = frozenset({"main.py"})
_ENTRYPOINT_CONTENT_MARKERS: Tuple[str, ...] = (
    "@app.get(", "@app.post(", "@app.put(", "@app.delete(", "@app.websocket(",
    "@app.middleware(",
    "@router.get(", "@router.post(", "@router.put(", "@router.delete(",
    "@router.websocket(",
    'if __name__ == "__main__"', "if __name__ == '__main__'",
    "register_tool(", "register_analyst_tools(", "register_schema(",
)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _to_relpath(file_path: str, workspace_root: str) -> str:
    """Workspace-relative, forward-slash form of an indexed file path.

    Allowlist patterns and reported candidates are both workspace-relative, so
    every indexed file (stored as an absolute path) must be relativized before
    matching or returning — skipping this would make a pattern like
    ``jobs/*.py`` silently never match anything. Falls back to the normalized
    absolute path if it unexpectedly doesn't fall under ``workspace_root``
    (defensive; a relative-glob match on it will simply never fire).
    """
    norm_file = _norm(file_path)
    norm_root = _norm(workspace_root).rstrip("/") if workspace_root else ""
    if norm_root and norm_file == norm_root:
        return ""
    if norm_root and norm_file.startswith(norm_root + "/"):
        return norm_file[len(norm_root) + 1:]
    return norm_file


def _is_filename_entrypoint(file_path: str) -> bool:
    """True if the file's name/basename alone identifies it as an entrypoint."""
    base = os.path.basename(_norm(file_path))
    if base in _ENTRYPOINT_BASENAMES:
        return True
    return any(fnmatch.fnmatch(base, pattern) for pattern in _TEST_FILENAME_PATTERNS)


def _matches_content_entrypoint_markers(content: Optional[str]) -> bool:
    """True if file content contains a known entrypoint decorator/statement/call.

    ``content is None`` (unreadable, jail-violating, or over the size cap) is
    treated as "no marker found" — a read failure can only under-detect an
    entrypoint, never wrongly flag a real entrypoint as dead.
    """
    if content is None:
        return False
    return any(marker in content for marker in _ENTRYPOINT_CONTENT_MARKERS)


def _bounded_jailed_read(path: str, workspace_root: str) -> Optional[str]:
    """Read a file from disk, confined to ``workspace_root`` and size-capped.

    Returns ``None`` on a jail violation, an oversized file, or any I/O error —
    mirrors ``tools/analyst_tools.py``'s ``_jailed_disk_read`` shape. Duplicated
    rather than imported: ``core/`` modules do not depend on ``tools/`` (only the
    reverse), so this small, already-validated pattern is repeated here instead
    of introducing a layering inversion.
    """
    try:
        resolved = pathlib.Path(path).resolve()
        jail = pathlib.Path(workspace_root).resolve()
        if not resolved.is_relative_to(jail):
            logger.warning("_bounded_jailed_read: path %s escapes workspace jail %s", path, workspace_root)
            return None
        size = resolved.stat().st_size
        if size > _CONTENT_MAX_BYTES:
            logger.debug("_bounded_jailed_read: %s exceeds %d byte cap (%d bytes)", path, _CONTENT_MAX_BYTES, size)
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _load_allowlist_patterns(
    project_id: Optional[str],
    workspace_root: Optional[str],
    session_id: Optional[str] = None,
) -> List[str]:
    """Read ``.ailienant/dead-code-allowlist.json``; fail open to ``[]`` on any problem.

    Absent file, malformed JSON, a wrong top-level shape (not a flat array of
    strings), or a read-safe firewall rejection (binary/oversized/ignored) all
    yield an empty list — the hardcoded entrypoint set alone still applies.
    Never raises: an unusable allowlist only ever disables the *extension*, not
    the underlying feature.
    """
    if not workspace_root:
        return []
    reader = make_safe_reader(project_id, workspace_root, session_id)
    candidate = str(pathlib.Path(workspace_root) / _ALLOWLIST_REL_PATH)
    content = reader(candidate)
    if not content or not content.strip():
        return []
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Dead-code allowlist at %s is malformed JSON; hardcoded entrypoints only: %s",
            candidate, exc, exc_info=True,
        )
        return []
    if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
        logger.warning(
            "Dead-code allowlist at %s is not a flat array of strings; hardcoded entrypoints only.",
            candidate,
        )
        return []
    return data[:_ALLOWLIST_MAX_PATTERNS]


def _build_allowlist_spec(patterns: Tuple[str, ...]) -> "Optional[pathspec.PathSpec[Any]]":
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def _is_allowlisted(rel_path: str, spec: "Optional[pathspec.PathSpec[Any]]") -> bool:
    if spec is None:
        return False
    return bool(spec.match_file(rel_path))


def _resolve_in_degree(
    edges: Tuple[Tuple[str, str], ...], indexed_files: Tuple[str, ...]
) -> Dict[str, int]:
    """Resolved in-degree per indexed file: ``target_dependency`` resolved to a
    concrete file first, so a dotted Python module target counts toward the file
    it names, not toward an unresolved string. Seeding from ``indexed_files``
    (not observed targets) is what excludes external packages — a target that
    never resolves to an indexed file never enters this map and is therefore
    never iterated as a dead-code candidate downstream.
    """
    indexed = set(indexed_files)
    norm_indexed = {_norm(f): f for f in indexed_files}
    py_index = _build_python_suffix_index(indexed_files)
    in_deg: Dict[str, int] = dict.fromkeys(indexed_files, 0)
    for _source, target in edges:
        for resolved in _resolved_target_files(target, indexed, norm_indexed, py_index):
            in_deg[resolved] = in_deg.get(resolved, 0) + 1
    return in_deg


def compute_dead_code_sync(
    edges: Tuple[Tuple[str, str], ...],
    indexed_files: Tuple[str, ...],
    workspace_root: str,
    allowlist_patterns: Tuple[str, ...] = (),
    *,
    content_reader: Callable[[str], Optional[str]],
) -> List[Dict[str, object]]:
    """Pure(-ish) candidate scan for one project's graph snapshot.

    ``content_reader`` is called at most once per candidate, only after the
    zero-I/O gates (in-degree, filename, allowlist) already let a file through —
    a file with in-degree > 0, i.e. most of an indexed codebase, is never opened.
    Deterministic, sorted output.
    """
    in_deg = _resolve_in_degree(edges, indexed_files)
    spec = _build_allowlist_spec(allowlist_patterns)

    orphans: List[Dict[str, object]] = []
    for file_path in sorted(indexed_files):
        if in_deg.get(file_path, 0) > 0:
            continue
        if _is_filename_entrypoint(file_path):
            continue
        rel = _to_relpath(file_path, workspace_root)
        if _is_allowlisted(rel, spec):
            continue
        if _matches_content_entrypoint_markers(content_reader(file_path)):
            continue
        orphans.append({"file": rel, "in_degree": 0})
    return orphans


async def compute_dead_code(
    project_id: str,
    workspace_root: str,
    session_id: Optional[str] = None,
    *,
    content_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Dict[str, object]]:
    """Fetch the project graph + allowlist and run the scan off the event loop.

    Nothing file-system-related happens before the ``asyncio.to_thread`` dispatch:
    the default reader is bound here but only ever invoked from inside the thread.
    """
    edges = await catalog_db.get_all_edges(project_id)
    indexed = await catalog_db.list_indexed_files(project_id)
    allowlist = _load_allowlist_patterns(project_id, workspace_root, session_id)

    reader = content_reader or (lambda path: _bounded_jailed_read(path, workspace_root))

    return await asyncio.to_thread(
        compute_dead_code_sync,
        tuple(edges),
        tuple(indexed),
        workspace_root,
        tuple(allowlist),
        content_reader=reader,
    )
