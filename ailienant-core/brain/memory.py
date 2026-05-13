"""
brain/memory.py — Process-pool-safe indexing bridge.

All functions are module-level so ProcessPoolExecutor can pickle them.
Phase 3 extends this module with LanceDB vector indexing and GraphRAG topology extraction.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.contracts import IndexingRequest, IndexingResult

logger = logging.getLogger("MEMORY_WORKER")

# Per-process singleton — initialized once by _worker_init(), never shared across processes.
_worker_ast: Optional[Any] = None


def _worker_init() -> None:
    """Called once per worker process by ProcessPoolExecutor(initializer=_worker_init)."""
    global _worker_ast
    from core.ast_engine import ASTEngine
    _worker_ast = ASTEngine()


def _count_top_level_symbols(tree: Any) -> int:
    if tree is None:
        return 0
    return sum(1 for node in tree.root_node.children if node.is_named)


def index_file_sync(req: IndexingRequest) -> IndexingResult:
    """Worker entry point: parse file AST, return a picklable result.

    Never raises — returns IndexingResult(success=False, error=...) on any exception
    so the asyncio caller always gets a result, never an unhandled worker exception.
    """
    global _worker_ast
    if _worker_ast is None:
        _worker_init()  # lazy fallback if pool was created without initializer
    try:
        tree = _worker_ast.parse(  # type: ignore[union-attr]
            req.file_path, req.content, req.language_id
        )
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=_count_top_level_symbols(tree),
            language_id=req.language_id,
            success=True,
        )
    except Exception as exc:
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=0,
            language_id=req.language_id,
            success=False,
            error=str(exc),
        )
