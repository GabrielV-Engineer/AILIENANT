# tools/validation/result.py
"""Phase 3.4.4 — Shared validation result types for the Micro-Isolate pipeline."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ValidationError(BaseModel):
    """One diagnostic emitted by an AST or LSP layer."""

    layer: Literal["AST", "LSP"]
    line: Optional[int] = None
    column: Optional[int] = None
    message: str


class ValidationResult(BaseModel):
    """Outcome of a single validation layer (AST or LSP)."""

    is_valid: bool
    errors: List[ValidationError] = Field(default_factory=list)
    prune_reason: Optional[str] = None


class PipelineResult(BaseModel):
    """Aggregated outcome of the fail-fast Micro-Isolate pipeline."""

    passed: bool
    failed_layer: Optional[Literal["AST", "LSP"]] = None
    errors: List[ValidationError] = Field(default_factory=list)
    prune_reason: Optional[str] = None
