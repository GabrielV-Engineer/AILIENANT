"""Hermetic gate for the TCI-stratified routing study.

No live model, no Docker. Pure-function tests feed synthetic ProblemMetrics
straight into build_routing_study to exercise the H2 arithmetic, the anchored
bucketing, and the strict-pairing invariant. A single stubbed sweep proves the
runner wires per-arm metrics into the table. The real token asymmetry between
routing and forced-cloud is a live-only property and is not faked here.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

import core.telemetry as telemetry
from core.telemetry import log_routing_decision
from core.token_ledger import token_ledger

from tests.benchmark.arms import AblationArm
from tests.benchmark.executors import SubprocessPythonExecutor
from tests.benchmark.metrics import ProblemMetrics
from tests.benchmark.oracle import BenchmarkOracle, load_corpus
from tests.benchmark.problems import BenchmarkProblem
from tests.benchmark.routing_study import (
    TCI_BUCKETS,
    bucket_for_tci,
    build_routing_study,
)
from tests.benchmark.runner import BenchmarkRunner, TaskRunner

_ROUTING = AblationArm.G4.value
_BASELINE = AblationArm.G4_FORCE_CLOUD.value


def _metric(
    problem_id: str,
    arm: str,
    *,
    tci: Optional[float],
    verdict: str,
    tokens_local: float = 0.0,
    tokens_cloud: float = 0.0,
) -> ProblemMetrics:
    return ProblemMetrics(
        arm=arm,
        problem_id=problem_id,
        tokens_local=tokens_local,
        tokens_cloud=tokens_cloud,
        est_usd=0.0,
        tci=tci,
        css=50.0,
        latency_s=0.0,
        verdict=verdict,
    )


def _pair(
    problem_id: str,
    *,
    tci: float,
    routing_verdict: str,
    baseline_verdict: str,
    routing_tokens: float = 0.0,
    baseline_tokens: float = 0.0,
) -> List[ProblemMetrics]:
    """A paired routing/baseline metric for one problem at a single anchor TCI."""
    return [
        _metric(
            problem_id, _ROUTING, tci=tci, verdict=routing_verdict,
            tokens_cloud=routing_tokens,
        ),
        _metric(
            problem_id, _BASELINE, tci=tci, verdict=baseline_verdict,
            tokens_cloud=baseline_tokens,
        ),
    ]


# --------------------------------------------------------------------------- #
# Bucketing                                                                     #
# --------------------------------------------------------------------------- #


def test_bucket_boundaries() -> None:
    """TCI maps to [0,40) / [40,75) / [75,100] with the high band closed."""
    assert bucket_for_tci(0.0) == "[0,40)"
    assert bucket_for_tci(39.9) == "[0,40)"
    assert bucket_for_tci(40.0) == "[40,75)"
    assert bucket_for_tci(74.9) == "[40,75)"
    assert bucket_for_tci(75.0) == "[75,100]"
    assert bucket_for_tci(100.0) == "[75,100]"


# --------------------------------------------------------------------------- #
# Resolve@3 and token efficiency                                                #
# --------------------------------------------------------------------------- #


def test_resolve_at_3_per_bucket() -> None:
    """Resolve@3 equals resolved/n per (bucket, arm)."""
    metrics: List[ProblemMetrics] = []
    # Mid bucket: routing resolves 2/3, baseline resolves 3/3.
    metrics += _pair("p1", tci=50.0, routing_verdict="passed", baseline_verdict="passed")
    metrics += _pair("p2", tci=55.0, routing_verdict="passed", baseline_verdict="passed")
    metrics += _pair("p3", tci=60.0, routing_verdict="failed", baseline_verdict="passed")

    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    routing_mid = table.cell("[40,75)", _ROUTING)
    baseline_mid = table.cell("[40,75)", _BASELINE)
    assert routing_mid is not None and baseline_mid is not None
    assert routing_mid.n == 3 and routing_mid.resolved == 2
    assert routing_mid.resolve_at_3 == pytest.approx(2 / 3)
    assert baseline_mid.resolve_at_3 == pytest.approx(1.0)


def test_token_efficiency_ratio() -> None:
    """tokens_per_resolved is tokens/resolved, and None when nothing resolved."""
    metrics = _pair(
        "p1", tci=50.0, routing_verdict="passed", baseline_verdict="failed",
        routing_tokens=120.0, baseline_tokens=300.0,
    )
    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    routing_mid = table.cell("[40,75)", _ROUTING)
    baseline_mid = table.cell("[40,75)", _BASELINE)
    assert routing_mid is not None and baseline_mid is not None
    assert routing_mid.tokens_per_resolved == pytest.approx(120.0)  # 120 / 1
    assert baseline_mid.tokens_per_resolved is None  # resolved 0 → undefined


# --------------------------------------------------------------------------- #
# H2 savings and retention                                                      #
# --------------------------------------------------------------------------- #


def test_h2_savings_and_retention() -> None:
    """Savings/retention ratios and threshold flags compute at the boundaries."""
    # Routing spends 60% of baseline tokens (boundary pass) at equal resolves.
    metrics = _pair(
        "p1", tci=50.0, routing_verdict="passed", baseline_verdict="passed",
        routing_tokens=60.0, baseline_tokens=100.0,
    )
    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    overall = table.overall
    assert overall.token_savings_ratio == pytest.approx(0.60)
    assert overall.meets_savings is True  # 0.60 <= 0.60
    assert overall.resolve_retention == pytest.approx(1.0)
    assert overall.meets_retention is True  # 1.0 >= 0.95

    # A regime that overspends and under-resolves fails both thresholds.
    bad = _pair(
        "q1", tci=50.0, routing_verdict="failed", baseline_verdict="passed",
        routing_tokens=90.0, baseline_tokens=100.0,
    )
    bad_table = build_routing_study(bad, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    assert bad_table.overall.meets_savings is False  # 0.90 > 0.60
    assert bad_table.overall.meets_retention is False  # 0.0 / 1.0 < 0.95


# --------------------------------------------------------------------------- #
# Anchored bucketing (Finding 2)                                                #
# --------------------------------------------------------------------------- #


def test_problem_id_anchoring() -> None:
    """A problem is filed by its routing-arm TCI even if the baseline TCI differs."""
    # Routing TCI 39.5 → [0,40); baseline TCI 40.5 would be [40,75) on its own.
    metrics = [
        _metric("p1", _ROUTING, tci=39.5, verdict="passed"),
        _metric("p1", _BASELINE, tci=40.5, verdict="passed"),
    ]
    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)

    lo_routing = table.cell("[0,40)", _ROUTING)
    lo_baseline = table.cell("[0,40)", _BASELINE)
    mid_baseline = table.cell("[40,75)", _BASELINE)
    assert lo_routing is not None and lo_baseline is not None
    # Both arms land in the low bucket; the baseline does NOT leak into mid.
    assert lo_routing.n == 1
    assert lo_baseline.n == 1
    assert mid_baseline is not None and mid_baseline.n == 0


# --------------------------------------------------------------------------- #
# Strict pairing (Finding 3)                                                    #
# --------------------------------------------------------------------------- #


def test_strict_pairing_invariant() -> None:
    """Buckets stay paired; unpaired and no-TCI problems are dropped and counted."""
    metrics: List[ProblemMetrics] = []
    metrics += _pair("paired", tci=50.0, routing_verdict="passed", baseline_verdict="passed")
    # Missing baseline → dropped_unpaired.
    metrics.append(_metric("lonely", _ROUTING, tci=50.0, verdict="passed"))
    # Routing TCI is None → dropped_no_tci (baseline present but unanchorable).
    metrics.append(_metric("blind", _ROUTING, tci=None, verdict="passed"))
    metrics.append(_metric("blind", _BASELINE, tci=50.0, verdict="passed"))

    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    assert table.dropped_unpaired == 1
    assert table.dropped_no_tci == 1

    for bucket in TCI_BUCKETS:
        routing_cell = table.cell(bucket, _ROUTING)
        baseline_cell = table.cell(bucket, _BASELINE)
        assert routing_cell is not None and baseline_cell is not None
        assert routing_cell.n == baseline_cell.n
    assert table.overall.routing.n == table.overall.baseline.n == 1


# --------------------------------------------------------------------------- #
# Full grid                                                                     #
# --------------------------------------------------------------------------- #


def test_table_covers_all_three_buckets() -> None:
    """Every bucket emits cells for both arms even when empty (always 3x2)."""
    metrics = _pair("p1", tci=50.0, routing_verdict="passed", baseline_verdict="passed")
    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)

    assert len(table.strata) == 3
    for bucket in TCI_BUCKETS:
        assert table.cell(bucket, _ROUTING) is not None
        assert table.cell(bucket, _BASELINE) is not None

    empty = next(s for s in table.strata if s.bucket == "[0,40)")
    assert empty.routing.n == 0
    assert empty.token_savings_ratio is None
    assert empty.resolve_retention is None
    assert empty.meets_savings is None
    assert empty.meets_retention is None


# --------------------------------------------------------------------------- #
# Render                                                                        #
# --------------------------------------------------------------------------- #


def test_render_table_lists_buckets_and_arms() -> None:
    """render() materializes the TCI-bucket x tokens x Resolve@3 table."""
    metrics = _pair(
        "p1", tci=80.0, routing_verdict="passed", baseline_verdict="passed",
        routing_tokens=50.0, baseline_tokens=100.0,
    )
    table = build_routing_study(metrics, routing_arm=_ROUTING, baseline_arm=_BASELINE)
    rendered = table.render()

    for bucket in TCI_BUCKETS:
        assert bucket in rendered
    assert _ROUTING in rendered
    assert _BASELINE in rendered
    assert "Resolve@3" in rendered
    assert "overall" in rendered


# --------------------------------------------------------------------------- #
# Stubbed live sweep (wiring only — Finding 4)                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _restore_telemetry_conn() -> Iterator[None]:
    saved = telemetry._conn
    yield
    telemetry._conn = saved


@pytest.fixture
def mock_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant_start(
        self: Any, workspace_root: str, project_id: str, session_id: str
    ) -> None:
        self._is_complete = True
        if self._complete_event is not None:
            self._complete_event.set()

    async def _fake_dependents(target: str, project_id: str = "") -> List[str]:
        return ["src/processor.py"]

    async def _ok_preflight(self: Any) -> None:
        return None

    monkeypatch.setattr("core.indexer.LazyIndexer.start", _instant_start)
    monkeypatch.setattr("core.db.get_dependents", _fake_dependents)
    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _ok_preflight)


def _golden_runner(problem: BenchmarkProblem, tci: float) -> TaskRunner:
    assert problem.corpus_problem is not None
    golden = dict(problem.corpus_problem.golden_patch)

    async def _run(session_id: str, _p: BenchmarkProblem) -> Dict[str, str]:
        token_ledger.record_local(prompt=50, completion=50)
        log_routing_decision(
            session_id, "planner", "coder", "golden stub", css=70.0, tci=tci
        )
        return golden

    return _run


def test_run_study_builds_table_from_stub(
    mock_infra: None, tmp_path: Any
) -> None:
    """run_study sweeps both arms over the corpus and returns a wired table."""
    corpus_root, problems = load_corpus("v1")
    problem = BenchmarkProblem.from_corpus(problems[0], corpus_root)
    oracle = BenchmarkOracle(corpus_root, SubprocessPythonExecutor())
    runner = BenchmarkRunner(
        task_runner=_golden_runner(problem, tci=80.0),
        oracle=oracle,
        telemetry_db_path=str(tmp_path / "tel.sqlite"),
    )

    table = asyncio.run(runner.run_study([problem]))
    assert table.routing_arm == _ROUTING
    assert table.baseline_arm == _BASELINE
    # Both arms ran the single problem and the oracle judged the golden patch.
    hi_routing = table.cell("[75,100]", _ROUTING)
    hi_baseline = table.cell("[75,100]", _BASELINE)
    assert hi_routing is not None and hi_baseline is not None
    assert hi_routing.n == 1 and hi_baseline.n == 1
    assert hi_routing.resolved == 1 and hi_baseline.resolved == 1
    assert table.dropped_no_tci == 0 and table.dropped_unpaired == 0
