# tools/validation/pipeline.py
"""Phase 3.4.4 — Fail-fast Micro-Isolate pipeline (AST -> LSP).

Phase 3.4.3b's MCTS daemon will call this pipeline before evaluate_nightmare().
On any failed layer the daemon should set MCTSNode.reward = 0.0 and prune.
"""
from __future__ import annotations

import logging

from tools.validation.ast_filter import validate_ast
from tools.validation.lsp_filter import validate_lsp
from tools.validation.result import PipelineResult

logger = logging.getLogger("VALIDATION_PIPELINE")


async def validate_delta(
    content: str,
    file_path: str,
    lsp_timeout: float = 5.0,
) -> PipelineResult:
    """Run AST -> LSP fail-fast. Returns passed=True if both layers pass."""
    ast_r = validate_ast(content, file_path)
    if not ast_r.is_valid:
        logger.info("pipeline FAIL@AST: %s", ast_r.prune_reason)
        return PipelineResult(
            passed=False,
            failed_layer="AST",
            errors=ast_r.errors,
            prune_reason=ast_r.prune_reason,
        )

    lsp_r = await validate_lsp(content, file_path, timeout=lsp_timeout)
    if not lsp_r.is_valid:
        logger.info("pipeline FAIL@LSP: %s", lsp_r.prune_reason)
        return PipelineResult(
            passed=False,
            failed_layer="LSP",
            errors=lsp_r.errors,
            prune_reason=lsp_r.prune_reason,
        )

    logger.debug("pipeline PASS: %s", file_path)
    return PipelineResult(passed=True)
