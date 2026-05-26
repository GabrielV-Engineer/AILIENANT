# ailienant-core/tools/inline_patch_validator.py
#
# Phase 7.11.1 (ADR-706 §4.5a) — Speculative AST validation for streaming inline edits.
#
# The frontend Cmd+K inline-mutation manager streams a sequence of typed deltas
# ({"kind":"INSERT"|"DELETE","offset":int,"length":int,"text":str}) accumulating
# into a partial replacement. Before each batch is broadcast, the orchestrator
# asks this validator: "does the speculative post-patch buffer survive a parse?"
#
# The check is INTENTIONALLY tolerant of mid-stream artifacts (open brackets,
# unterminated strings, a half-typed return statement). It rejects ONLY hard
# structural breakage that no continuation can repair (invalid token, illegal
# indentation jump unrelated to EOF, root-level tree-sitter ERROR runs).
#
# This honors the ADR-706 mandate to "reconcile through the existing VFS +
# `apply_patch` AST validation" by reusing core.ast_engine.ASTEngine.parse for
# 20+ tree-sitter grammars and the stdlib `ast` module for Python. No I/O,
# no LLM call, no side effects — pure function over (baseline, deltas).
#
# Cognitive isolation: imports `ast` (stdlib) and `core.ast_engine` only.
# MUST NOT import `brain.personality` — this module sits on the logic-agent
# side of the Phase 4.1.5 fence.
from __future__ import annotations

import ast
import logging
from typing import Any, Dict, Iterable, List, Optional

from core.ast_engine import ASTEngine

logger = logging.getLogger("INLINE_PATCH_VALIDATOR")

# Module-level singleton (cheap reuse — ASTEngine is thread-safe).
_ast_engine: ASTEngine = ASTEngine()

# Python `SyntaxError.msg` substrings that are *expected* mid-stream and must
# NOT cause a rejection. The frontend will keep streaming and the next batch
# will close the construct. Anything outside this allow-list is a hard fail.
_PYTHON_INCREMENTAL_SUBSTRINGS: tuple[str, ...] = (
    "unexpected EOF",
    "EOF while scanning",
    "unterminated string literal",
    "unterminated triple-quoted",
    "expected an indented block",
    "incomplete input",
)


def _apply_deltas(baseline: str, deltas: Iterable[Dict[str, Any]]) -> str:
    """Reconstruct the speculative post-patch buffer.

    Deltas are applied in the order received, each indexed against the
    *current* buffer (so the frontend can stream offset adjustments that
    already account for prior INSERTs / DELETEs). Out-of-range offsets are
    clamped — a misaligned delta yields a buffer that may fail validation,
    which is the correct signal (don't broadcast that frame).
    """
    buf = baseline
    for d in deltas:
        kind = str(d.get("kind", "")).upper()
        offset = int(d.get("offset", 0))
        if offset < 0:
            offset = 0
        if offset > len(buf):
            offset = len(buf)
        if kind == "INSERT":
            text = str(d.get("text", ""))
            buf = buf[:offset] + text + buf[offset:]
        elif kind == "DELETE":
            length = int(d.get("length", 0))
            end = min(offset + max(length, 0), len(buf))
            buf = buf[:offset] + buf[end:]
        # ABORT / unknown kinds: no-op (the caller already handles them upstream).
    return buf


def _validate_python(content: str) -> bool:
    """Tolerant Python validation — accepts EOF-class incompleteness."""
    try:
        ast.parse(content)
        return True
    except SyntaxError as exc:
        msg = (exc.msg or "").lower()
        for needle in _PYTHON_INCREMENTAL_SUBSTRINGS:
            if needle.lower() in msg:
                return True
        logger.debug("Python validator: hard syntax error: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 — defensive; ast.parse should not raise others
        logger.debug("Python validator: unexpected error: %s", exc)
        return False


def _tree_has_root_error_run(tree: Any) -> bool:
    """Detect a *root-level* run of ERROR nodes in a tree-sitter tree.

    Tree-sitter ERROR nodes near the streaming boundary are tolerable — every
    interactive editor produces them while a token is being typed. What we
    reject is the whole root being an ERROR (= the parser could not find ANY
    valid construct), which means the buffer is structurally broken in a way
    no continuation can fix.
    """
    if tree is None:
        return False
    root = getattr(tree, "root_node", None)
    if root is None:
        return False
    # A root that is itself ERROR is a definitive structural break.
    if getattr(root, "type", "") == "ERROR":
        return True
    children = list(getattr(root, "children", []) or [])
    if not children:
        return False
    # Reject only when *every* top-level child is an ERROR (parser gave up
    # entirely). Mixed valid + ERROR children = incremental, allowed.
    return all(getattr(c, "type", "") == "ERROR" for c in children)


def validate_partial_syntax(
    file_path: str,
    baseline_content: str,
    deltas: List[Dict[str, Any]],
    *,
    language_id: Optional[str] = None,
) -> bool:
    """Speculative AST validation for a streaming patch batch.

    Returns True when the speculative post-patch buffer either (a) parses
    cleanly, or (b) fails only with an incremental / EOF-style anomaly that a
    later stream chunk can repair. Returns False on a hard structural break
    (invalid token, root-level tree-sitter ERROR run) that no continuation can
    rescue — the caller should ABORT the stream rather than emit that frame.

    `language_id=None` (or any language the AST engine cannot parse) returns
    True: we cannot validate it, so we must not block it. The downstream
    PatchActuator's SHA-256 stale-guard still protects the commit boundary.
    """
    speculative = _apply_deltas(baseline_content, deltas)

    if language_id == "python" or file_path.endswith(".py"):
        return _validate_python(speculative)

    if language_id is None:
        # Mirror _validate_python_syntax in tools/patch_tool.py: unknown
        # extensions are accepted (cannot validate ≠ must reject).
        return True

    tree = _ast_engine.parse(file_path, speculative, language_id)
    if tree is None:
        # ASTEngine returned None — either the grammar package is unavailable
        # or the language is not in _LANG_MAP. Cannot validate → must not block.
        return True
    return not _tree_has_root_error_run(tree)
