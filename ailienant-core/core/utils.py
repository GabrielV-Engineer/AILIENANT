# ailienant-core/core/utils.py
#
# Phase 2.22.6 — Polyglot file heuristic.

import os

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
