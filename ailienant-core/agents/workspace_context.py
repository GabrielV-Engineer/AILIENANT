# ailienant-core/agents/workspace_context.py
"""Phase 7.12 — Workspace-shape context injection (Issues 4 & 8).

Neither the Planner nor the Analyst (Natt) previously saw the *shape* of the
active workspace — only dirty buffers, semantic-search hits and GraphRAG
neighbours. This helper produces a small, deterministic, HARD-bounded overview
(a depth-limited folder tree + the contents of root manifest files) so both
agents gain workspace-shape awareness without a full index.

Hard limits (guard against monorepo / huge-tree token explosion + O(N) latency):
  * ``max_depth = 3``   — never descend past 3 levels below the root.
  * ``max_files = 100`` — absolute truncation; the walk short-circuits once hit.
  * ``budget = 2048``   — final char cap on the whole block (ADR-703 G4 spirit).

Never raises — assembly degrades gracefully so a read failure cannot crash an
agent turn.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from core.storage_paths import is_ailienant_internal_path

logger = logging.getLogger("WORKSPACE_CONTEXT")

# Defaults — overridable per call but every call is still capped.
MAX_DEPTH: int = 3
MAX_FILES: int = 100
OVERVIEW_BUDGET: int = 2048
_MANIFEST_CAP: int = 600  # per-manifest char cap

# Directories that never carry useful workspace-shape signal (and would explode
# the file count). Pruned in-place during the walk.
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".hg", ".svn", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "out", ".next", ".nuxt", ".turbo", "coverage",
    ".idea", ".vscode", "target", "bin", "obj", ".cache", "site-packages",
})

# Root files worth injecting verbatim (project intent + dependency surface).
_ROOT_MANIFESTS: tuple[str, ...] = (
    "README.md", "pyproject.toml", "package.json",
)


def _build_tree(root: Path, max_depth: int, max_files: int) -> List[str]:
    """Depth- and count-bounded folder tree. Short-circuits at ``max_files``."""
    lines: List[str] = []
    files_seen = 0
    root_str = str(root)
    for current, dirnames, filenames in os.walk(root_str):
        # Depth of `current` relative to root (root itself == depth 0).
        rel = os.path.relpath(current, root_str)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []  # do not descend further
        # Prune noise dirs in-place (also stops os.walk recursing into them).
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        indent = "  " * depth
        if depth > 0:
            lines.append(f"{indent}{os.path.basename(current)}/")
        for fname in sorted(filenames):
            # Never expose AILIENANT's own runtime artifacts (telemetry log,
            # rotated siblings) as organizable user content — they self-mutate.
            if is_ailienant_internal_path(fname):
                continue
            if files_seen >= max_files:
                lines.append(f"{indent}  … (truncated at {max_files} files)")
                return lines
            lines.append(f"{indent}  {fname}")
            files_seen += 1
    return lines


def _read_manifests(root: Path, cap: int) -> List[str]:
    """Read present root manifests, each truncated to ``cap`` chars."""
    blocks: List[str] = []
    for name in _ROOT_MANIFESTS:
        fp = root / name
        try:
            if not fp.is_file():
                continue
            text = fp.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:  # pragma: no cover — defensive
            logger.debug("workspace manifest read failed for %s: %s", name, exc)
            continue
        if text:
            blocks.append(f"--- {name} ---\n{text[:cap]}")
    return blocks


def build_workspace_overview(
    workspace_root: str,
    *,
    max_depth: int = MAX_DEPTH,
    max_files: int = MAX_FILES,
    budget: int = OVERVIEW_BUDGET,
) -> str:
    """Return a HARD-bounded workspace-shape overview, or ``""`` when unavailable.

    The block is plain text (tree + manifests). Callers are responsible for any
    sandbox-tag wrapping required by their own context contract (e.g. ADR-703).
    """
    if not workspace_root:
        return ""
    try:
        root = Path(workspace_root)
        if not root.is_dir():
            return ""
    except OSError:
        return ""

    sections: List[str] = []
    try:
        tree = _build_tree(root, max_depth, max_files)
        if tree:
            sections.append(f"Workspace root: {root.name}\n" + "\n".join(tree))
        sections.extend(_read_manifests(root, _MANIFEST_CAP))
    except Exception as exc:  # noqa: BLE001 — never crash an agent turn
        logger.debug("workspace overview assembly failed: %s", exc)
        return ""

    if not sections:
        return ""
    return "\n\n".join(sections)[:budget]
