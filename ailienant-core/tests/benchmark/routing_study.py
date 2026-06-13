"""TCI-stratified routing study aggregator.

Turns a flat list of per-problem, per-arm metrics into a table that compares a
TCI-routing arm against a forced-cloud baseline, stratified by a problem's
cognitive-complexity (TCI) bucket. The table answers the cost-efficiency
question: does TCI-aware routing keep the same resolution rate while spending
fewer tokens than always routing to cloud?

Two rigor guarantees:

* **Anchored bucketing.** A problem's bucket is decided once, by the routing
  arm's TCI, and both arms' metrics for that problem are filed under that single
  bucket. Without anchoring, run-to-run TCI jitter (cloud models are not bitwise
  deterministic at temperature zero) could file the same problem under different
  buckets for each arm and silently compare disjoint problem sets.
* **Strict pairing.** A problem contributes to a bucket only when both arms
  produced a metric. An unpaired problem (a budget abort or a crashed arm) is
  dropped, so every bucket holds the same problem set for both arms and the
  per-bucket counts stay paired.

The Token Efficiency Ratio (tokens per resolved problem) is reported per bucket
rather than as a single number: a system that resolves the cheap problems and
fails the expensive ones would otherwise show a flattering aggregate ratio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from tests.benchmark.metrics import ProblemMetrics

# TCI runs on a 0-100 scale. The bucket edges partition complexity into low,
# medium, and high bands; the high band is closed so a maximum TCI lands in it.
_BUCKET_LO = "[0,40)"
_BUCKET_MID = "[40,75)"
_BUCKET_HI = "[75,100]"
TCI_BUCKETS: Tuple[str, str, str] = (_BUCKET_LO, _BUCKET_MID, _BUCKET_HI)

# H2 thresholds: routing must keep at least this share of the baseline's
# resolution rate while spending at most this share of its tokens.
_RETENTION_FLOOR = 0.95
_SAVINGS_CEILING = 0.60


def bucket_for_tci(tci: float) -> str:
    """Map a TCI score (0-100) to its complexity bucket label."""
    if tci < 40.0:
        return _BUCKET_LO
    if tci < 75.0:
        return _BUCKET_MID
    return _BUCKET_HI


@dataclass(frozen=True)
class StratumCell:
    """Aggregate outcome for one arm within one TCI bucket."""

    arm: str
    bucket: str
    n: int
    resolved: int
    resolve_at_3: float
    tokens_total: float
    tokens_local: float
    tokens_cloud: float
    est_usd: float
    # Tokens spent per resolved problem; None when nothing resolved (undefined).
    tokens_per_resolved: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-native projection for serialization into a report."""
        return {
            "arm": self.arm,
            "bucket": self.bucket,
            "n": self.n,
            "resolved": self.resolved,
            "resolve_at_3": self.resolve_at_3,
            "tokens_total": self.tokens_total,
            "tokens_local": self.tokens_local,
            "tokens_cloud": self.tokens_cloud,
            "est_usd": self.est_usd,
            "tokens_per_resolved": self.tokens_per_resolved,
        }


@dataclass(frozen=True)
class H2Stratum:
    """One bucket's routing-vs-baseline comparison for the cost-efficiency test."""

    bucket: str
    routing: StratumCell
    baseline: StratumCell
    # routing tokens / baseline tokens; None when the baseline spent nothing.
    token_savings_ratio: Optional[float]
    # routing Resolve@3 / baseline Resolve@3; None when the baseline resolved nothing.
    resolve_retention: Optional[float]
    # True when the ratio meets its threshold; None when the ratio is undefined.
    meets_savings: Optional[bool]
    meets_retention: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-native projection for serialization into a report."""
        return {
            "bucket": self.bucket,
            "routing": self.routing.to_dict(),
            "baseline": self.baseline.to_dict(),
            "token_savings_ratio": self.token_savings_ratio,
            "resolve_retention": self.resolve_retention,
            "meets_savings": self.meets_savings,
            "meets_retention": self.meets_retention,
        }


@dataclass(frozen=True)
class RoutingStudyTable:
    """The full TCI-bucket x tokens x Resolve@3 table for a routing study."""

    routing_arm: str
    baseline_arm: str
    cells: Tuple[StratumCell, ...]
    strata: Tuple[H2Stratum, ...]
    overall: H2Stratum
    dropped_no_tci: int
    dropped_unpaired: int

    def cell(self, bucket: str, arm: str) -> Optional[StratumCell]:
        """Return the cell for a (bucket, arm) pair, or None if absent."""
        for c in self.cells:
            if c.bucket == bucket and c.arm == arm:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-native projection for serialization into a report.

        Strata are emitted in the fixed bucket order so the projection is stable
        across runs regardless of how the source metrics were ordered.
        """
        return {
            "routing_arm": self.routing_arm,
            "baseline_arm": self.baseline_arm,
            "strata": [s.to_dict() for s in self.strata],
            "overall": self.overall.to_dict(),
            "dropped_no_tci": self.dropped_no_tci,
            "dropped_unpaired": self.dropped_unpaired,
        }

    def render(self) -> str:
        """Render the human-readable TCI-bucket x tokens x Resolve@3 table."""
        header = (
            f"Routing study: {self.routing_arm} (routing) vs "
            f"{self.baseline_arm} (baseline)"
        )
        col = (
            f"{'TCI bucket':<12} "
            f"{'tokens(' + self.routing_arm + ')':>20} "
            f"{'tokens(' + self.baseline_arm + ')':>22} "
            f"{'Resolve@3(' + self.routing_arm + ')':>24} "
            f"{'Resolve@3(' + self.baseline_arm + ')':>26} "
            f"{'token_savings':>14} {'retention':>11}"
        )
        lines = [header, col, "-" * len(col)]
        for stratum in self.strata:
            lines.append(self._render_row(stratum))
        lines.append("-" * len(col))
        lines.append(self._render_row(self.overall, label="overall"))
        if self.dropped_no_tci or self.dropped_unpaired:
            lines.append(
                f"(dropped: no_tci={self.dropped_no_tci}, "
                f"unpaired={self.dropped_unpaired})"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_row(stratum: H2Stratum, label: Optional[str] = None) -> str:
        name = label if label is not None else stratum.bucket
        savings = _fmt_ratio(stratum.token_savings_ratio)
        retention = _fmt_ratio(stratum.resolve_retention)
        return (
            f"{name:<12} "
            f"{stratum.routing.tokens_total:>20.0f} "
            f"{stratum.baseline.tokens_total:>22.0f} "
            f"{stratum.routing.resolve_at_3:>24.3f} "
            f"{stratum.baseline.resolve_at_3:>26.3f} "
            f"{savings:>14} {retention:>11}"
        )


def _fmt_ratio(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.3f}"


@dataclass
class _Accumulator:
    """Running tally for one (bucket, arm) cell while building the table."""

    n: int = 0
    resolved: int = 0
    tokens_local: float = 0.0
    tokens_cloud: float = 0.0
    est_usd: float = 0.0

    def add(self, m: ProblemMetrics) -> None:
        self.n += 1
        if m.verdict == "passed":
            self.resolved += 1
        self.tokens_local += m.tokens_local
        self.tokens_cloud += m.tokens_cloud
        self.est_usd += m.est_usd

    def to_cell(self, arm: str, bucket: str) -> StratumCell:
        tokens_total = self.tokens_local + self.tokens_cloud
        resolve_at_3 = (self.resolved / self.n) if self.n else 0.0
        tokens_per_resolved = (
            (tokens_total / self.resolved) if self.resolved else None
        )
        return StratumCell(
            arm=arm,
            bucket=bucket,
            n=self.n,
            resolved=self.resolved,
            resolve_at_3=resolve_at_3,
            tokens_total=tokens_total,
            tokens_local=self.tokens_local,
            tokens_cloud=self.tokens_cloud,
            est_usd=self.est_usd,
            tokens_per_resolved=tokens_per_resolved,
        )


def _make_stratum(
    bucket: str, routing: StratumCell, baseline: StratumCell
) -> H2Stratum:
    token_savings_ratio: Optional[float] = None
    meets_savings: Optional[bool] = None
    if baseline.tokens_total > 0.0:
        token_savings_ratio = routing.tokens_total / baseline.tokens_total
        meets_savings = token_savings_ratio <= _SAVINGS_CEILING

    resolve_retention: Optional[float] = None
    meets_retention: Optional[bool] = None
    if baseline.resolve_at_3 > 0.0:
        resolve_retention = routing.resolve_at_3 / baseline.resolve_at_3
        meets_retention = resolve_retention >= _RETENTION_FLOOR

    return H2Stratum(
        bucket=bucket,
        routing=routing,
        baseline=baseline,
        token_savings_ratio=token_savings_ratio,
        resolve_retention=resolve_retention,
        meets_savings=meets_savings,
        meets_retention=meets_retention,
    )


def build_routing_study(
    metrics: List[ProblemMetrics],
    *,
    routing_arm: str,
    baseline_arm: str,
) -> RoutingStudyTable:
    """Aggregate paired per-arm metrics into a TCI-stratified routing study.

    Metrics are grouped by ``problem_id``; each problem's bucket is anchored to
    the routing arm's TCI and both arms' metrics are filed there. A problem is
    dropped when the routing arm produced no metric or a None TCI
    (``dropped_no_tci``) or when the baseline arm is missing (``dropped_unpaired``),
    keeping every bucket's counts paired across the two arms.
    """
    by_problem: Dict[str, Dict[str, ProblemMetrics]] = {}
    for m in metrics:
        if m.arm not in (routing_arm, baseline_arm):
            continue
        by_problem.setdefault(m.problem_id, {})[m.arm] = m

    # bucket -> arm -> accumulator
    tallies: Dict[str, Dict[str, _Accumulator]] = {
        bucket: {routing_arm: _Accumulator(), baseline_arm: _Accumulator()}
        for bucket in TCI_BUCKETS
    }

    dropped_no_tci = 0
    dropped_unpaired = 0
    for arms in by_problem.values():
        routing_metric = arms.get(routing_arm)
        if routing_metric is None or routing_metric.tci is None:
            dropped_no_tci += 1
            continue
        baseline_metric = arms.get(baseline_arm)
        if baseline_metric is None:
            dropped_unpaired += 1
            continue
        bucket = bucket_for_tci(routing_metric.tci)
        tallies[bucket][routing_arm].add(routing_metric)
        tallies[bucket][baseline_arm].add(baseline_metric)

    cells: List[StratumCell] = []
    strata: List[H2Stratum] = []
    overall_routing = _Accumulator()
    overall_baseline = _Accumulator()
    for bucket in TCI_BUCKETS:
        routing_cell = tallies[bucket][routing_arm].to_cell(routing_arm, bucket)
        baseline_cell = tallies[bucket][baseline_arm].to_cell(baseline_arm, bucket)
        cells.append(routing_cell)
        cells.append(baseline_cell)
        strata.append(_make_stratum(bucket, routing_cell, baseline_cell))
        _accumulate_into(overall_routing, tallies[bucket][routing_arm])
        _accumulate_into(overall_baseline, tallies[bucket][baseline_arm])

    overall = _make_stratum(
        "overall",
        overall_routing.to_cell(routing_arm, "overall"),
        overall_baseline.to_cell(baseline_arm, "overall"),
    )

    return RoutingStudyTable(
        routing_arm=routing_arm,
        baseline_arm=baseline_arm,
        cells=tuple(cells),
        strata=tuple(strata),
        overall=overall,
        dropped_no_tci=dropped_no_tci,
        dropped_unpaired=dropped_unpaired,
    )


def _accumulate_into(target: _Accumulator, source: _Accumulator) -> None:
    target.n += source.n
    target.resolved += source.resolved
    target.tokens_local += source.tokens_local
    target.tokens_cloud += source.tokens_cloud
    target.est_usd += source.est_usd
