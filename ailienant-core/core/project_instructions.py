"""Freeform project-instructions channel — ``AILIENANT.md``.

A project can ship a prose instruction file that every task reads, the way a
repository ships a contributor guide. This complements the structured
``.ailienant.json`` rules (machine-checkable constraints) with free text the
model reads as standing guidance: stack conventions, domain vocabulary, "always
do X / never do Y" notes that do not fit a JSON schema.

Resolution prefers ``<workspace>/.ailienant/AILIENANT.md`` and falls back to a
flat ``<workspace>/AILIENANT.md``. The read goes through the same path-confined
``read_safe`` firewall every other context read uses, and the content is capped
so a large file can never exhaust the context window (token hygiene).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from core.vfs_middleware import make_safe_reader

logger = logging.getLogger("PROJECT_INSTRUCTIONS")

# Candidate locations, in priority order, relative to the workspace root.
_CANDIDATES: tuple[str, ...] = (".ailienant/AILIENANT.md", "AILIENANT.md")

# Character cap (~2k tokens). Generous for prose guidance, bounded so the prefix
# stays small relative to the rest of the prompt. A larger file is head-sliced.
_MAX_CHARS: int = 8000

_HEADING: str = "## Project Instructions (AILIENANT.md)"
_TRUNCATION_NOTE: str = "\n\n[... project instructions truncated ...]"


def get_project_instructions(
    project_id: Optional[str],
    workspace_root: Optional[str],
    session_id: Optional[str] = None,
    *,
    max_chars: int = _MAX_CHARS,
) -> str:
    """Return a formatted project-instructions block, or ``""`` when none exists.

    Never raises: a missing file, an unreadable path, or an empty file all yield
    the empty string so prompt assembly proceeds unchanged (zero tokens added).
    """
    if not workspace_root:
        return ""

    reader = make_safe_reader(project_id, workspace_root, session_id)
    for rel in _CANDIDATES:
        candidate = str(Path(workspace_root) / rel)
        content = reader(candidate)
        if not content or not content.strip():
            continue
        text = content.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + _TRUNCATION_NOTE
            logger.info("Project instructions head-sliced to %d chars (%s).", max_chars, rel)
        else:
            logger.debug("Project instructions loaded from %s.", rel)
        return f"{_HEADING}\n\n{text}"
    return ""
