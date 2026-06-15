"""Raw per-problem metric record and telemetry readback.

A single task emits several routing-decision rows under its session id, and only
the planner's per-problem assessment carries both the complexity (TCI) and
context-sufficiency (CSS) scores. Those scores are computed once and propagated
through the run state, so every scored row for a session is equivalent; the
reader selects the latest scored row and treats the absence of any scored row as
a wiring failure rather than silently defaulting to zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from core.telemetry import recent_routing_decisions

# Read a window wide enough that a just-finished serial run's rows are present.
_ROUTING_READ_LIMIT = 200


class BenchmarkMetricError(RuntimeError):
    """Raised when expected telemetry is missing for a measured problem."""


@dataclass
class ProblemMetrics:
    """Raw, unaggregated metrics for one problem run under one arm."""

    arm: str
    problem_id: str
    tokens_local: float
    tokens_cloud: float
    est_usd: float
    tci: Optional[float]
    css: Optional[float]
    latency_s: float
    # Populated once the benchmark oracle exists; unscored in the scaffold.
    verdict: Optional[str] = None


def collect_routing(session_id: str) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(tci, css)`` for one problem run, isolated by its unique session.

    The unique session guarantees no other problem's rows leak in. Among this
    session's rows, the canonical assessment is any row carrying both scores
    (they are propagated from the same run state); the latest such row is used.
    """
    rows = recent_routing_decisions(limit=_ROUTING_READ_LIMIT)
    scored = [
        row
        for row in rows
        if row.get("session_id") == session_id
        and row.get("tci_score") is not None
        and row.get("css_score") is not None
    ]
    if not scored:
        raise BenchmarkMetricError(
            f"no TCI/CSS routing row recorded for session {session_id!r}"
        )
    # recent_routing_decisions returns newest-first.
    canonical = scored[0]
    return canonical["tci_score"], canonical["css_score"]
