# tools/validation/ast_filter.py
"""Phase 3.4.4 — Layer 1 structural validation (RAM-only, ~O(1) per file).

Python uses stdlib `ast.parse()` for richer SyntaxError diagnostics.
TS/TSX delegates to the existing tree-sitter ASTEngine in core/ast_engine.py.
Unsupported extensions pass through (is_valid=True).
"""
from __future__ import annotations

import ast
import logging
import os
from typing import Dict, FrozenSet, Optional

from tools.validation.result import ValidationError, ValidationResult

logger = logging.getLogger("AST_FILTER")

_PY_EXTS: FrozenSet[str] = frozenset({".py"})
# ext -> language_id used by core.ast_engine._LANG_MAP.
_TS_LANG_BY_EXT: Dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescriptreact",
}


def validate_ast(content: str, file_path: str) -> ValidationResult:
    """Return ValidationResult for one file. Unsupported extensions pass through."""
    ext: str = os.path.splitext(file_path)[1].lower()
    if ext in _PY_EXTS:
        return _validate_python(content, file_path)
    if ext in _TS_LANG_BY_EXT:
        return _validate_ts(content, file_path, _TS_LANG_BY_EXT[ext])
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
