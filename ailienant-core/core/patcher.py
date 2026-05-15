# ailienant-core/core/patcher.py
#
# Phase 2.22 — Atomic Code Patcher.
#
# Pure text-processing engine. No VFS, no os, no sys, no open().
# Called by Phase 5 tool wrappers (make_apply_patch_tool).

from core.exceptions import PatchError


def _normalize(text: str) -> str:
    """Normalize line endings to LF and strip trailing whitespace per line."""
    return "\n".join(
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )


def apply_search_replace(content: str, search: str, replace: str) -> str:
    """Apply a search/replace patch atomically.

    Algorithm (two-pass):
      Pass 1 — Exact string match. Fastest path; requires exactly 1 occurrence.
      Pass 2 — Normalized whitespace match (CRLF→LF, strip trailing spaces).
               Handles editor-induced line-ending and whitespace drift.

    Raises:
      PatchError: search block not found (0 matches) or ambiguous (2+ matches).
    """
    # --- Pass 1: exact match ---
    exact_count = content.count(search)
    if exact_count == 1:
        return content.replace(search, replace, 1)
    if exact_count > 1:
        raise PatchError(
            f"Ambiguous patch: search block found {exact_count} times (exact match). "
            "Provide more surrounding context lines to make the anchor unique."
        )

    # --- Pass 2: normalized whitespace match ---
    norm_content = _normalize(content)
    norm_search = _normalize(search)
    norm_replace = _normalize(replace)

    norm_count = norm_content.count(norm_search)
    if norm_count == 1:
        return norm_content.replace(norm_search, norm_replace, 1)
    if norm_count > 1:
        raise PatchError(
            f"Ambiguous patch: search block found {norm_count} times after normalization. "
            "Provide more surrounding context lines to make the anchor unique."
        )

    # Not found — build a diagnostic snippet
    search_lines = search.splitlines()
    snippet = search_lines[0][:80] if search_lines else "(empty search block)"
    raise PatchError(
        f"Search block not found (exact or normalized). "
        f"First line of search block: {snippet!r}. "
        "Verify the target file content matches the expected block."
    )
