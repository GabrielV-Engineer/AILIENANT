"""Hermetic gate for the machine-readable benchmark report.

No live model, no Docker. Pure-function tests feed synthetic ProblemMetrics into
build_report to exercise the Wilson interval, the per-arm aggregates, the two
hypothesis verdicts (including the zero-resolution edge cases that a naive
inequality would mis-pass), the ablation deltas, and schema-valid serialization.
A single stubbed sweep proves the runner wires the full five-arm matrix into a
report.
"""
from __future__ import annotations

import asyncio
import json
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
from tests.benchmark.report import (
    REPORT_SCHEMA,
    BenchmarkReport,
    build_report,
    serialize_report,
    validate_report,
    wilson_interval,
    write_report,
)
from tests.benchmark.runner import BenchmarkRunner, TaskRunner

_G1 = AblationArm.G1.value
_G2 = AblationArm.G2.value
_G3 = AblationArm.G3.value
_G4 = AblationArm.G4.value
_FORCE = AblationArm.G4_FORCE_CLOUD.value
_SCHEMA_PATH = Path(__file__).parent / "report.schema.json"


def _m(
    problem_id: str,
    arm: str,
    *,
    verdict: Optional[str],
    tci: Optional[float] = 50.0,
    tokens_local: float = 0.0,
    tokens_cloud: float = 0.0,
    est_usd: float = 0.0,
) -> ProblemMetrics:
    return ProblemMetrics(
        arm=arm,
        problem_id=problem_id,
        tokens_local=tokens_local,
        tokens_cloud=tokens_cloud,
        est_usd=est_usd,
        tci=tci,
        css=50.0,
        latency_s=0.0,
        verdict=verdict,
    )


def _full_report(metrics: List[ProblemMetrics]) -> BenchmarkReport:
    return build_report(metrics, corpus_sha="deadbeef", complete=True)


# --------------------------------------------------------------------------- #
# Wilson interval                                                              #
# --------------------------------------------------------------------------- #


def test_wilson_interval() -> None:
    """Known Wilson bounds; n==0 is (0,0); bounds clamp to [0,1]."""
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3)
    assert hi == pytest.approx(0.9433, abs=1e-3)
    assert lo < 0.8 < hi

    assert wilson_interval(0, 0) == (0.0, 0.0)

    # A zero proportion pins the lower bound at 0; an all-success proportion pins
    # the upper bound at exactly 1 (a Wilson identity at phat=1), with a lower
    # bound strictly inside the unit interval.
    zlo, _zhi = wilson_interval(0, 5)
    assert zlo == pytest.approx(0.0, abs=1e-9)
    plo, phi = wilson_interval(5, 5)
    assert phi == pytest.approx(1.0)
    assert 0.0 < plo < 1.0


# --------------------------------------------------------------------------- #
# Group aggregates                                                             #
# --------------------------------------------------------------------------- #


def test_group_aggregates() -> None:
    """Per-arm n/resolved/resolve_at_3; unscored rows excluded from n."""
    metrics = [
        _m("p1", _G1, verdict="passed", tokens_local=10.0),
        _m("p2", _G1, verdict="passed", tokens_local=10.0),
        _m("p3", _G1, verdict="failed", tokens_local=10.0),
        _m("p4", _G1, verdict=None, tokens_local=10.0),  # unscored
    ]
    report = _full_report(metrics)
    g1 = next(g for g in report.groups if g.arm == _G1)
    assert g1.n == 3  # the unscored row does not count
    assert g1.resolved == 2
    assert g1.resolve_at_3 == pytest.approx(2 / 3)
    assert g1.tokens_total == pytest.approx(40.0)  # all four rows' tokens
    assert 0.0 < g1.wilson_lo < g1.resolve_at_3 < g1.wilson_hi < 1.0


# --------------------------------------------------------------------------- #
# H1 — precision uplift                                                        #
# --------------------------------------------------------------------------- #


def _h1_pair(problem_id: str, *, tci: float, g1: str, g4: str) -> List[ProblemMetrics]:
    return [_m(problem_id, _G1, tci=tci, verdict=g1), _m(problem_id, _G4, tci=tci, verdict=g4)]


def test_h1_holds_and_fails() -> None:
    """H1 passes at the 1.25x boundary and fails below it; no data -> None."""
    # Control 2/4 = 0.50 -> threshold 0.625; treatment 3/4 = 0.75 -> holds.
    passing: List[ProblemMetrics] = []
    passing += _h1_pair("a", tci=70.0, g1="passed", g4="passed")
    passing += _h1_pair("b", tci=70.0, g1="passed", g4="passed")
    passing += _h1_pair("c", tci=70.0, g1="failed", g4="passed")
    passing += _h1_pair("d", tci=70.0, g1="failed", g4="failed")
    h1 = _full_report(passing).h1
    assert h1.n == 4
    assert h1.holds is True
    assert h1.threshold == pytest.approx(0.625)

    # Treatment drops to 2/4 = 0.50 < 0.625 -> refuted.
    failing: List[ProblemMetrics] = []
    failing += _h1_pair("a", tci=70.0, g1="passed", g4="passed")
    failing += _h1_pair("b", tci=70.0, g1="passed", g4="passed")
    failing += _h1_pair("c", tci=70.0, g1="failed", g4="failed")
    failing += _h1_pair("d", tci=70.0, g1="failed", g4="failed")
    assert _full_report(failing).h1.holds is False

    # No problem above the TCI floor -> undefined.
    below = _h1_pair("a", tci=40.0, g1="passed", g4="passed")
    none_h1 = _full_report(below).h1
    assert none_h1.n == 0
    assert none_h1.holds is None


def test_h1_both_fail_is_false_not_true() -> None:
    """The 0/0 trap: mutual failure is refuted, not a vacuous pass."""
    both_fail = _h1_pair("a", tci=70.0, g1="failed", g4="failed")
    h1 = _full_report(both_fail).h1
    assert h1.resolve_treatment == 0.0 and h1.resolve_baseline == 0.0
    assert h1.holds is False  # NOT True — 0.0 >= 1.25*0.0 must not pass

    # Control resolves nothing but treatment resolves something -> genuine uplift.
    uplift = _h1_pair("a", tci=70.0, g1="failed", g4="passed")
    assert _full_report(uplift).h1.holds is True


def test_h1_anchors_tci_gate_to_g4() -> None:
    """Inclusion is anchored to G4's TCI even when G1's would exclude the problem."""
    metrics = [
        _m("p1", _G4, tci=61.0, verdict="passed"),  # above floor -> included
        _m("p1", _G1, tci=59.0, verdict="passed"),  # below floor on its own
    ]
    h1 = _full_report(metrics).h1
    assert h1.n == 1  # anchored to G4=61, not split out by G1=59


# --------------------------------------------------------------------------- #
# H2 — cost efficiency                                                         #
# --------------------------------------------------------------------------- #


def test_h2_embeds_routing_study() -> None:
    """H2 holds at savings<=0.60 & retention>=0.95; strata carry token efficiency."""
    metrics = [
        _m("p1", _G4, tci=50.0, verdict="passed", tokens_cloud=60.0),
        _m("p1", _FORCE, tci=50.0, verdict="passed", tokens_cloud=100.0),
    ]
    report = _full_report(metrics)
    assert report.h2.holds is True
    assert report.h2.token_savings_ratio == pytest.approx(0.60)
    assert report.h2.resolve_retention == pytest.approx(1.0)

    mid = report.routing_study.cell("[40,75)", _G4)
    assert mid is not None and mid.tokens_per_resolved == pytest.approx(60.0)


# --------------------------------------------------------------------------- #
# Ablation deltas                                                             #
# --------------------------------------------------------------------------- #


def test_ablation_deltas() -> None:
    """The three named deltas, paired, with correct resolution/token signs."""
    metrics = [
        # G2 fails, G3 passes -> graph delta positive.
        _m("p1", _G2, verdict="failed", tokens_cloud=100.0),
        _m("p1", _G3, verdict="passed", tokens_cloud=120.0),
        _m("p1", _G4, verdict="passed", tokens_cloud=140.0),
        _m("p1", _FORCE, verdict="passed", tokens_cloud=200.0),
    ]
    deltas = {d.name: d for d in _full_report(metrics).ablation_deltas}
    assert set(deltas) == {"G2->G3", "G3->G4", "G4->G4_FORCE_CLOUD"}

    graph = deltas["G2->G3"]
    assert graph.isolates == "graph topology (GraphRAG)"
    assert graph.n == 1
    assert graph.delta_resolve_at_3 == pytest.approx(1.0)  # 1.0 - 0.0
    assert graph.delta_tokens_total == pytest.approx(20.0)  # 120 - 100

    # Routing arm spends fewer tokens than forced cloud at equal resolution.
    routing = deltas["G4->G4_FORCE_CLOUD"]
    assert routing.delta_resolve_at_3 == pytest.approx(0.0)
    assert routing.delta_tokens_total == pytest.approx(60.0)  # 200 - 140


# --------------------------------------------------------------------------- #
# Schema + serialization (the DoD)                                            #
# --------------------------------------------------------------------------- #


def _five_arm_metrics() -> List[ProblemMetrics]:
    metrics: List[ProblemMetrics] = []
    for arm in (_G1, _G2, _G3, _G4, _FORCE):
        metrics.append(_m("p1", arm, tci=70.0, verdict="passed", tokens_cloud=50.0))
        metrics.append(_m("p2", arm, tci=30.0, verdict="failed", tokens_cloud=20.0))
    return metrics


def test_report_validates_against_schema() -> None:
    """build_report().to_dict() is a schema-valid document (the DoD)."""
    obj = _full_report(_five_arm_metrics()).to_dict()
    validate_report(obj)  # raises on any drift


def test_committed_schema_matches_code() -> None:
    """The committed report.schema.json equals the in-code REPORT_SCHEMA."""
    on_disk = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert on_disk == REPORT_SCHEMA


def test_write_report_roundtrip(tmp_path: Path) -> None:
    """Atomic write lands a valid file in place and leaves no temp behind."""
    report = _full_report(_five_arm_metrics())
    out = tmp_path / "report.json"
    write_report(report, out)

    assert out.exists()
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded == report.to_dict()
    validate_report(reloaded)
    # No stray temp file from the atomic rename.
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_report_rejects_non_finite(tmp_path: Path) -> None:
    """A non-finite float is refused (allow_nan=False) and leaves nothing behind."""
    poisoned = build_report(
        _five_arm_metrics(),
        corpus_sha="x",
        complete=True,
        indexing_time_s=float("inf"),
    )
    out = tmp_path / "report.json"
    with pytest.raises(ValueError):
        write_report(poisoned, out)
    assert not out.exists()
    assert list(tmp_path.glob("*.tmp")) == []

    # The same guard fires in the pure serializer.
    with pytest.raises(ValueError):
        serialize_report(poisoned.to_dict())


def test_routing_study_to_dict_schema() -> None:
    """The embedded routing study is JSON-native and structurally complete."""
    report = _full_report(_five_arm_metrics())
    rs = report.routing_study.to_dict()
    # JSON-native (no dataclasses leaked) and strictly serializable.
    json.dumps(rs, allow_nan=False)
    assert len(rs["strata"]) == 3
    assert set(rs["overall"]) == {
        "bucket",
        "routing",
        "baseline",
        "token_savings_ratio",
        "resolve_retention",
        "meets_savings",
        "meets_retention",
    }


# --------------------------------------------------------------------------- #
# Stubbed full-matrix sweep (wiring only)                                      #
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


def test_run_report_builds_full_matrix_from_stub(
    mock_infra: None, tmp_path: Any
) -> None:
    """run_report sweeps all five arms and returns a schema-valid report."""
    corpus_root, problems = load_corpus("v1")
    problem = BenchmarkProblem.from_corpus(problems[0], corpus_root)
    oracle = BenchmarkOracle(corpus_root, SubprocessPythonExecutor())
    runner = BenchmarkRunner(
        task_runner=_golden_runner(problem, tci=80.0),
        oracle=oracle,
        telemetry_db_path=str(tmp_path / "tel.sqlite"),
    )

    report = asyncio.run(runner.run_report([problem]))
    assert report.complete is True
    assert report.corpus_sha == "a3f1c7e2"
    assert {g.arm for g in report.groups} == {_G1, _G2, _G3, _G4, _FORCE}
    validate_report(report.to_dict())
