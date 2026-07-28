# ailienant-core/core/utils.py
#
# Polyglot file heuristic + cross-project RAG relevance filtering.

import os
from typing import Iterable, List, Optional, Tuple

_POLYGLOT_EXTENSIONS: frozenset[str] = frozenset({
    ".html", ".vue", ".svelte",
    ".jsx", ".tsx",
    ".jinja", ".jinja2", ".j2",
    ".md", ".mdx",
    ".erb", ".ejs",
})


def is_polyglot_file(file_path: str) -> bool:
    """Return True if the file is likely to contain mixed-syntax content.

    Compound extension .blade.php is handled as a special case since
    os.path.splitext only captures the last suffix.
    """
    lower = file_path.lower()
    if lower.endswith(".blade.php"):
        return True
    _, ext = os.path.splitext(lower)
    return ext in _POLYGLOT_EXTENSIONS


def _top_level_segment(path: str) -> str:
    """The first path component, POSIX or Windows separators alike."""
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized.split("/", 1)[0] if normalized else ""


def filter_relevant_snippets(
    snippets: List[Tuple[str, str]],
    anchor_file: str,
    explicit_mentions: Optional[Iterable[str]] = None,
) -> List[Tuple[str, str]]:
    """Drop RAG snippets that live in an unrelated top-level project directory.

    A single opened workspace root spanning two unrelated projects (e.g. one
    folder containing both an unrelated existing app and a brand-new one)
    shares one `project_id` / vector index, so a semantic search scoped to the
    new project can legitimately surface top-k matches from the OTHER — the
    index has no per-project boundary, only a per-workspace one. Approximate
    the missing boundary with the cheapest available signal: the anchor
    file's top-level path segment. A snippet whose own top-level segment
    differs is treated as off-topic and dropped.

    Never drops a file the task explicitly named — an explicit reference
    always wins over this heuristic. Returns ``snippets`` unfiltered when
    ``anchor_file`` has no directory component (nothing to compare against,
    e.g. a flat single-file workspace).
    """
    if not snippets:
        return snippets
    normalized_anchor = anchor_file.replace("\\", "/").lstrip("/")
    if "/" not in normalized_anchor:
        # Anchor sits at the workspace root — no directory segment to scope
        # against (its "top-level segment" would just be its own filename,
        # which would wrongly exclude every subdirectory in the workspace).
        return snippets
    anchor_top = normalized_anchor.split("/", 1)[0]
    mentions = set(explicit_mentions or ())
    return [
        (path, content) for path, content in snippets
        if path in mentions or _top_level_segment(path) == anchor_top
    ]
