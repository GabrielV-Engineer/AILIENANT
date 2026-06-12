"""Measurement-hygiene guarantees for the benchmark harness.

Three non-negotiable controls keep the numbers honest:

* an embedding pre-flight that aborts the run when the semantic layer is down
  (a silent zero-retrieval run would invalidate every GraphRAG-using arm),
* a response-cache reset before every problem (a cache hit serves a prior answer
  for free and falsifies token accounting), and
* a cumulative spend ceiling so an unbounded sweep aborts cleanly instead of
  burning budget.
"""
from __future__ import annotations

from typing import Dict


class BenchmarkAbort(RuntimeError):
    """Raised to abort a run when a hard precondition fails."""


class BudgetExceeded(RuntimeError):
    """Raised when a sweep's cumulative spend reaches its ceiling."""


# Pinned for reproducibility. A global seam to force the temperature on every
# model call does not yet exist; the values are recorded here and enforcement is
# wired in a later phase.
SEED: int = 42
TEMPERATURE: float = 0.0


def configure_determinism() -> Dict[str, float]:
    """Return the determinism configuration the harness pins for a run."""
    return {"seed": float(SEED), "temperature": TEMPERATURE}


async def assert_embeddings_live() -> None:
    """Abort the run if the embedding backend is unreachable.

    Surfaces the existing indexer pre-flight reason as a hard abort so a run can
    never proceed into a silent zero-retrieval state.
    """
    from core.indexer import LazyIndexer

    reason = await LazyIndexer()._preflight_check()
    if reason is not None:
        raise BenchmarkAbort(reason)


def disable_response_cache() -> None:
    """Clear the semantic response cache so a repeated problem recomputes tokens."""
    from core.response_cache import response_cache

    response_cache.clear()


class BenchmarkBudget:
    """A spend ceiling measured against the delta from a sweep-start baseline.

    The token ledger is a cumulative global singleton the harness never resets,
    so the ceiling is checked against spend accumulated since the sweep began,
    not the absolute ledger total.
    """

    def __init__(self, ceiling_usd: float, baseline_usd: float) -> None:
        self._ceiling = ceiling_usd
        self._baseline = baseline_usd

    @classmethod
    def from_snapshot(
        cls, ceiling_usd: float, snapshot: Dict[str, float]
    ) -> "BenchmarkBudget":
        """Capture the baseline spend from a ledger snapshot taken at sweep start."""
        return cls(ceiling_usd, float(snapshot.get("estimated_invested_usd", 0.0)))

    def spent(self, snapshot: Dict[str, float]) -> float:
        """Cloud USD spent since the baseline."""
        return float(snapshot.get("estimated_invested_usd", 0.0)) - self._baseline

    def check(self, snapshot: Dict[str, float]) -> None:
        """Raise ``BudgetExceeded`` once cumulative spend reaches the ceiling."""
        spent = self.spent(snapshot)
        if spent >= self._ceiling:
            raise BudgetExceeded(
                f"benchmark budget ${self._ceiling:.2f} reached (spent ${spent:.4f})"
            )
