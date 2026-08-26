# tools/validation/ast_filter.py
"""Phase 3.4.4 — Layer 1 structural validation (RAM-only, ~O(1) per file).

Python uses stdlib `ast.parse()` for richer SyntaxError diagnostics. Every other
language delegates to the tree-sitter ASTEngine in core/ast_engine.py.

Language coverage is DERIVED, never hand-listed here: the extension → languageId
mapping comes from `shared.contracts.detect_language` (the same map the indexer
uses) and the languageId → grammar mapping from `core.ast_engine._LANG_MAP`. A
local copy would drift from the engine, and a validator that silently believes it
covers a language it does not is worse than no validator at all.

A file type no grammar covers still passes through, but says so in the log — an
unqualified `is_valid=True` reads as "checked and clean" when it means "not
checked", which is precisely the confusion this module exists to remove.
"""
from __future__ import annotations

import ast
import logging
import os
from typing import FrozenSet, Optional

from tools.validation.result import ValidationError, ValidationResult

logger = logging.getLogger("AST_FILTER")

_PY_EXTS: FrozenSet[str] = frozenset({".py"})


def resolve_grammar_language(file_path: str) -> Optional[str]:
    """Return the tree-sitter-backed languageId for *file_path*, or ``None``.

    ``None`` means no grammar covers this file type — the caller must treat that
    as "unverified", not as "valid".
    """
    from core.ast_engine import _LANG_MAP  # deferred — heavy module, lazy grammars
    from shared.contracts import detect_language

    language_id = detect_language(file_path)
    return language_id if language_id in _LANG_MAP else None


def validate_ast(content: str, file_path: str) -> ValidationResult:
    """Return ValidationResult for one file. Unsupported file types pass through."""
    ext: str = os.path.splitext(file_path)[1].lower()
    if ext in _PY_EXTS:
        return _validate_python(content, file_path)
    language_id = resolve_grammar_language(file_path)
    if language_id is not None:
        return _validate_ts(content, file_path, language_id)
    logger.debug(
        "AST: %s has no grammar coverage — passing through UNVERIFIED, not validated.",
        file_path,
    )
    return ValidationResult(is_valid=True)


def _validate_python(content: str, file_path: str) -> ValidationResult:
    try:
        ast.parse(content, filename=file_path)
        return ValidationResult(is_valid=True)
    except SyntaxError as exc:
        msg: str = exc.msg or "SyntaxError"
        line: Optional[int] = exc.lineno
        col: Optional[int] = exc.offset
        logger.info("AST(py): %s:%s: %s", file_path, line, msg)
        return ValidationResult(
            is_valid=False,
            errors=[ValidationError(layer="AST", line=line, column=col, message=msg)],
            prune_reason=f"AST(py): {file_path}:{line}: {msg}",
        )


def _validate_ts(content: str, file_path: str, language_id: str) -> ValidationResult:
    # Deferred: ASTEngine is heavy and grammars are lazy-loaded.
    from core.ast_engine import ASTEngine
    engine: ASTEngine = ASTEngine()
    tree = engine.parse(file_path, content, language_id)
    if tree is None:
        return ValidationResult(
            is_valid=False,
            errors=[ValidationError(
                layer="AST",
                message="parser unavailable or grammar load failed",
            )],
            prune_reason=f"AST({language_id}): parser unavailable",
        )
    if tree.root_node.has_error:
        start_row: int = int(tree.root_node.start_point[0]) + 1
        start_col: int = int(tree.root_node.start_point[1]) + 1
        return ValidationResult(
            is_valid=False,
            errors=[ValidationError(
                layer="AST",
                line=start_row,
                column=start_col,
                message=f"{language_id}: tree-sitter detected structural error",
            )],
            prune_reason=f"AST({language_id}): structural error in {file_path}",
        )
    return ValidationResult(is_valid=True)
