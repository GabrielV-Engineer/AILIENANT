"""Retrieval strategy objects for the ablation harness.

Each strategy owns the injectable override callables that disable a specific
retrieval layer. The agents read these from ``config["configurable"]`` (keys
``graph_fn`` / ``planner_retrieval_fn`` / ``coder_retrieval_fn``) and fall back to
their real bound methods when a key is absent — so production runs untouched while
an arm degrades retrieval explicitly, with no monkeypatching of internals.
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, Tuple


class RetrievalStrategy(Protocol):
    """A retrieval capability configuration: a name and the overrides it injects."""

    name: str

    def overrides(self) -> Dict[str, Any]: ...


# Replacement callables injected via config. Each mirrors the real retrieval
# signature (keyword-driven) and returns an empty-but-well-formed result.


async def _no_graph(*args: Any, **kwargs: Any) -> Any:
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
    *args: Any, **kwargs: Any
) -> Tuple[float, List[str], List[str]]:
    """Return empty results for search_with_paths (no vector retrieval)."""
    return 0.0, [], []


async def _no_vector_snippets(*args: Any, **kwargs: Any) -> List[Tuple[str, str]]:
    """Return empty results for search_snippets (no vector retrieval)."""
    return []


class FullRetrievalStrategy:
    """G4 — no overrides; full pipeline active."""

    name: str = "full"

    def overrides(self) -> Dict[str, Any]:
        return {}


class VectorOnlyRetrievalStrategy:
    """G2 — graph topology silenced, vector retrieval left live."""

    name: str = "vector_only"

    def overrides(self) -> Dict[str, Any]:
        return {"graph_fn": _no_graph}


class ZeroShotRetrievalStrategy:
    """G1 — all retrieval suppressed (graph + vector, planner + coder)."""

    name: str = "zero_shot"

    def overrides(self) -> Dict[str, Any]:
        return {
            "graph_fn": _no_graph,
            "planner_retrieval_fn": _no_vector_paths,
            "coder_retrieval_fn": _no_vector_snippets,
        }
