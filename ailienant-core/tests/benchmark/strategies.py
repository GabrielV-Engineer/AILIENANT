"""Retrieval strategy objects for the ablation harness.

Each strategy owns the mock.patch objects that disable a specific retrieval
layer. Production code is never modified; patches are scoped to a single
task run via apply_arm's ExitStack.
"""
from __future__ import annotations

from typing import Any, List, Protocol, Tuple
from unittest import mock


class RetrievalStrategy(Protocol):
    """A retrieval capability configuration: a name and the patches it requires."""

    name: str

    def patches(self) -> List[Any]: ...


# Replacement callables used by strategy patch objects.
# Each is an async method substitute (receives self as first arg).


async def _no_graph(self: Any, *args: Any, **kwargs: Any) -> Any:
    """Return an empty but well-formed DeepParseResult (no graph context)."""
    from core.memory.graphrag_extractor import DeepParseResult

    return DeepParseResult(
        target_files=[],
        parsed_files=[],
        context_block="",
        coverage_ratio=0.0,
        token_count=0,
    )


async def _no_vector_paths(
    self: Any, *args: Any, **kwargs: Any
) -> Tuple[float, List[str], List[str]]:
    """Return empty results for search_with_paths (no vector retrieval)."""
    return 0.0, [], []


async def _no_vector_snippets(
    self: Any, *args: Any, **kwargs: Any
) -> List[Tuple[str, str]]:
    """Return empty results for search_snippets (no vector retrieval)."""
    return []


class FullRetrievalStrategy:
    """G4 — no patches; full pipeline active."""

    name: str = "full"

    def patches(self) -> List[Any]:
        return []


class VectorOnlyRetrievalStrategy:
    """G2 — graph topology silenced, vector retrieval left live."""

    name: str = "vector_only"

    def patches(self) -> List[Any]:
        from tests.benchmark.arms import GRAPH_SEAM

        return [mock.patch(GRAPH_SEAM, _no_graph)]


class ZeroShotRetrievalStrategy:
    """G1 — all retrieval suppressed (graph + vector, planner + coder)."""

    name: str = "zero_shot"

    def patches(self) -> List[Any]:
        from tests.benchmark.arms import (
            CODER_VECTOR_SEAM,
            GRAPH_SEAM,
            PLANNER_VECTOR_SEAM,
        )

        return [
            mock.patch(GRAPH_SEAM, _no_graph),
            mock.patch(PLANNER_VECTOR_SEAM, _no_vector_paths),
            mock.patch(CODER_VECTOR_SEAM, _no_vector_snippets),
        ]
