# ailienant-core/tools/patch_tool.py
#
# Phase 2.22.1 & 2.22.2 — AtomicPatch Tool Schema & Context Anchoring Engine.
#
# 2.22.1: AtomicPatchInput Pydantic schema — strict field validation.
# 2.22.2: make_patch_file_tool factory — fuzzy fallback + AST boundary check.

import ast
import difflib
import logging
from typing import Any, Callable, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from core.exceptions import PatchError
from core.patcher import apply_search_replace

logger = logging.getLogger("PATCH_TOOL")

_FUZZY_THRESHOLD = 0.90


# ---------------------------------------------------------------------------
# 2.22.1 — Strict Input Schema
# ---------------------------------------------------------------------------

class AtomicPatchInput(BaseModel):
    file_path: str = Field(..., description="Target file path in the VFS.")
    search_block: str = Field(
        ...,
        description=(
            "The code to replace. Must be >= 10 non-whitespace chars to prevent "
            "ambiguous matches. Empty string is rejected."
        ),
    )
    replace_block: str = Field(
        default="",
        description="Replacement code. Empty string = deletion.",
    )
    ast_context_node: Optional[str] = Field(
        default=None,
        description="Optional AST scope hint (e.g., 'class UserManager') for logging.",
    )

    @field_validator("search_block")
    @classmethod
    def search_block_not_empty(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError(
                f"search_block must have at least 10 non-whitespace characters "
                f"(got {len(v.strip())}). A too-short anchor risks ambiguous matches."
            )
        return v


# ---------------------------------------------------------------------------
# 2.22.2 — Context Anchoring Engine
# ---------------------------------------------------------------------------

def _fuzzy_find_and_replace(content: str, search: str, replace: str) -> str:
    """Sliding-window difflib fallback. Raises PatchError if best ratio < 0.90.

    Scans content in windows of len(search_lines) lines, scoring each against
    search via SequenceMatcher. If the best window exceeds _FUZZY_THRESHOLD, the
    window is replaced with `replace`. Otherwise PatchError is raised.
    """
    content_lines = content.splitlines(keepends=True)
    search_lines = search.splitlines(keepends=True)
    n = len(search_lines)

    if n == 0 or not content_lines:
        raise PatchError("Search block or content is empty; cannot fuzzy-match.")

    best_ratio = 0.0
    best_start = -1
    search_str = "".join(search_lines)

    for i in range(max(1, len(content_lines) - n + 1)):
        window = "".join(content_lines[i : i + n])
        ratio = difflib.SequenceMatcher(None, window, search_str).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio < _FUZZY_THRESHOLD:
        snippet = search_lines[0][:80].rstrip() if search_lines else ""
        raise PatchError(
            f"Search block not found (best fuzzy ratio: {best_ratio:.2f} < {_FUZZY_THRESHOLD}). "
            f"First line: {snippet!r}."
        )

    logger.info(
        "PATCH_TOOL: fuzzy match accepted (ratio=%.3f, start_line=%d).",
        best_ratio,
        best_start,
    )
    replace_lines = replace.splitlines(keepends=True)
    if replace and not replace.endswith("\n") and replace_lines:
        replace_lines[-1] += "\n"
    return "".join(
        content_lines[:best_start] + replace_lines + content_lines[best_start + n :]
    )


def _validate_python_syntax(patched: str, file_path: str) -> None:
    """If file_path ends in .py, parse and raise PatchError on SyntaxError."""
    if not file_path.endswith(".py"):
        return
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise PatchError(
            f"AST Validation Failed: SyntaxError at line {exc.lineno}. "
            "You likely hallucinated indentation or orphaned a brace."
        ) from exc


def make_patch_file_tool(
    vfs_read: Callable[[str], Optional[str]],
    vfs_write: Callable[[str, str], None],
) -> Any:
    """Factory: returns a LangChain tool that applies AtomicPatch via the VFS.

    Injection pattern mirrors tools/agent_tools.py — VFS callables are captured
    by closure so they never appear in the tool schema exposed to the LLM.
    """

    @tool(args_schema=AtomicPatchInput)  # type: ignore[misc]
    def patch_file(
        file_path: str,
        search_block: str,
        replace_block: str = "",
        ast_context_node: Optional[str] = None,
    ) -> str:
        """Apply a targeted search/replace patch to a file in the VFS.

        Matching order: exact -> normalized whitespace -> fuzzy (>0.90 ratio).
        Python files are AST-validated before the write is committed.
        """
        content = vfs_read(file_path)
        if content is None:
            raise PatchError(f"File not found in VFS: {file_path!r}.")

        if ast_context_node:
            logger.info("PATCH_TOOL: scope hint=%r for %s.", ast_context_node, file_path)

        patched: str
        try:
            patched = apply_search_replace(content, search_block, replace_block)
            logger.info("PATCH_TOOL: exact/normalized match succeeded for %s.", file_path)
        except PatchError as primary_err:
            # Fuzzy fallback only for "not found" — ambiguous errors must propagate.
            if "not found" in str(primary_err):
                logger.info("PATCH_TOOL: exact match failed — trying fuzzy fallback.")
                patched = _fuzzy_find_and_replace(content, search_block, replace_block)
            else:
                raise

        _validate_python_syntax(patched, file_path)
        vfs_write(file_path, patched)
        logger.info("PATCH_TOOL: patch written to %s.", file_path)
        return f"Patch applied successfully to {file_path!r}."

    return patch_file
