"""Reproducibility gate for the benchmark harness.

A benchmark is only defensible if two runs of the same frozen corpus measure the
same thing. Two properties guarantee that: the corpus records the exact pinned
revision it was cut from, and the report aggregator is a pure function whose
output depends only on the metrics — never on wall-clock or input ordering — so
the same metrics always serialize to byte-identical JSON.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import List

from tests.benchmark.arms import AblationArm
from tests.benchmark.hygiene import SEED, TEMPERATURE, configure_determinism
from tests.benchmark.metrics import ProblemMetrics
from tests.benchmark.oracle import load_corpus
from tests.benchmark.report import build_report, serialize_report

_ARMS = [a.value for a in AblationArm]


def _corpus_metrics() -> List[ProblemMetrics]:
    """A deterministic synthetic sweep across all arms and complexity bands."""
    metrics: List[ProblemMetrics] = []
    for i in range(6):
        tci = float(20 + i * 12)  # spans the low/mid/high TCI bands
        for arm in _ARMS:
            metrics.append(
                ProblemMetrics(
                    arm=arm,
                    problem_id=f"p{i}",
                    tokens_local=float(10 * i),
                    tokens_cloud=float(5 * i),
                    est_usd=0.001 * i,
                    tci=tci,
                    css=50.0,
                    latency_s=0.0,
                    verdict="passed" if (i + len(arm)) % 2 == 0 else "failed",
                )
            )
    return metrics


def test_corpus_sha_is_pinned_and_surfaced() -> None:
    """The frozen corpus carries a pinned SHA and the report surfaces it."""
    corpus_root, _problems = load_corpus("v1")  # raises if pinned_sha is empty
    meta = json.loads((corpus_root / "meta.json").read_text(encoding="utf-8"))
    pinned = meta["pinned_sha"]
    assert pinned  # non-empty

    report = build_report(_corpus_metrics(), corpus_sha=pinned, complete=True)
    assert report.corpus_sha == pinned
    assert report.to_dict()["corpus_sha"] == pinned


def test_report_builder_is_deterministic() -> None:
    """Identical metrics — in any order — serialize to byte-identical JSON."""
    metrics = _corpus_metrics()
    shuffled = list(metrics)
    random.Random(1234).shuffle(shuffled)

    first = serialize_report(
        build_report(metrics, corpus_sha="a3f1c7e2", complete=True).to_dict()
    )
    again = serialize_report(
        build_report(metrics, corpus_sha="a3f1c7e2", complete=True).to_dict()
    )
    from_shuffled = serialize_report(
        build_report(shuffled, corpus_sha="a3f1c7e2", complete=True).to_dict()
    )

    assert first == again
    assert first == from_shuffled  # canonical ordering defeats input-order drift


def test_seed_and_temp_are_pinned() -> None:
    """The report and the harness both declare the fixed-seed/zero-temp contract."""
    report = build_report(_corpus_metrics(), corpus_sha="a3f1c7e2", complete=True)
    assert report.seed == 42

    assert SEED == 42
    assert TEMPERATURE == 0.0
    assert configure_determinism() == {"seed": 42.0, "temperature": 0.0}
