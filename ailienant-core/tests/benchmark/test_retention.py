"""Gate for benchmark artifact retention (the count cap + LRU-by-mtime eviction).

No live model, no Docker. Exercises the pure ``prune_artifacts`` mechanism, the
fail-safe config reader, and the end-to-end bound: driving ``run_benchmark`` past
the cap leaves the artifact directory at exactly the cap. The runner is stubbed to
return a prebuilt valid report, so the test isolates the retention path from the
cognitive engine.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, List

import pytest

from core import benchmark_service
from core.benchmark.metrics import ProblemMetrics
from core.benchmark.report import BenchmarkReport, build_report, prune_artifacts


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def iso_bench(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the benchmark artifact dir, the global config, and run/slot state."""
    bench_dir = tmp_path / "benchmark"
    monkeypatch.setattr(benchmark_service, "BENCHMARK_DIR", bench_dir)
    monkeypatch.setattr(
        benchmark_service, "GLOBAL_CONFIG_PATH", tmp_path / ".ailienant.json"
    )
    monkeypatch.setattr(benchmark_service, "_runs", {})
    monkeypatch.setattr(benchmark_service, "_inflight", 0)
    monkeypatch.delenv("AILIENANT_GATEWAY_BENCH_CONCURRENCY", raising=False)
    return bench_dir


def _make_artifacts(directory: Path, count: int) -> List[Path]:
    """Create ``count`` ``*.json`` artifacts with strictly increasing mtimes.

    Explicit mtimes via ``os.utime`` make "which N survived" deterministic
    regardless of the filesystem's mtime resolution.
    """
    directory.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for i in range(count):
        p = directory / f"{i:032x}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))  # oldest first
        paths.append(p)
    return paths


def _valid_report() -> BenchmarkReport:
    """A minimal schema-valid report so the real ``write_report`` runs end-to-end."""
    metrics = [
        ProblemMetrics(
            arm="G1",
            problem_id="p1",
            tokens_local=10.0,
            tokens_cloud=0.0,
            est_usd=0.0,
            tci=50.0,
            css=50.0,
            latency_s=0.0,
            verdict="passed",
        )
    ]
    return build_report(metrics, corpus_sha="stub", complete=True)


# ---------------------------------------------------------------------------
# prune_artifacts — the pure mechanism
# ---------------------------------------------------------------------------


def test_prune_keeps_newest_and_returns_deleted(tmp_path: Path) -> None:
    paths = _make_artifacts(tmp_path, 5)
    deleted = prune_artifacts(tmp_path, 3)

    survivors = sorted(tmp_path.glob("*.json"))
    assert len(survivors) == 3
    # The 3 newest (highest mtime = last created) survive; the 2 oldest are gone.
    assert survivors == sorted(paths[2:])
    assert set(deleted) == set(paths[:2])


def test_prune_is_noop_under_cap(tmp_path: Path) -> None:
    _make_artifacts(tmp_path, 2)
    assert prune_artifacts(tmp_path, 5) == []
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_prune_is_idempotent_at_cap(tmp_path: Path) -> None:
    _make_artifacts(tmp_path, 4)
    first = prune_artifacts(tmp_path, 2)
    second = prune_artifacts(tmp_path, 2)
    assert len(first) == 2
    assert second == []
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_prune_tolerates_vanished_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _make_artifacts(tmp_path, 3)
    real_unlink = Path.unlink

    def _racy_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        # Simulate a concurrent pruner removing the oldest between glob and unlink.
        if self == paths[0]:
            real_unlink(self)
            raise FileNotFoundError(self)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _racy_unlink)
    deleted = prune_artifacts(tmp_path, 1)  # keep newest 1, delete paths[0] + paths[1]

    assert paths[0] in deleted  # already-gone at unlink time still counts as success
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_prune_only_targets_json(tmp_path: Path) -> None:
    _make_artifacts(tmp_path, 3)
    lock = tmp_path / ".retention.lock"
    lock.write_text("", encoding="utf-8")
    tmp = tmp_path / "0000.json.tmp"
    tmp.write_text("partial", encoding="utf-8")

    prune_artifacts(tmp_path, 1)

    assert lock.exists()
    assert tmp.exists()
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_prune_missing_directory_is_safe(tmp_path: Path) -> None:
    assert prune_artifacts(tmp_path / "absent", 5) == []


# ---------------------------------------------------------------------------
# _resolve_max_stored_runs — fail-safe config reader
# ---------------------------------------------------------------------------


def _write_config(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_config_default_when_absent(iso_bench: Path, tmp_path: Path) -> None:
    # GLOBAL_CONFIG_PATH points at a nonexistent file under iso_bench.
    assert benchmark_service._resolve_max_stored_runs() == 20


def test_config_honoured_when_present(iso_bench: Path, tmp_path: Path) -> None:
    _write_config(benchmark_service.GLOBAL_CONFIG_PATH, {"benchmark": {"max_stored_runs": 7}})
    assert benchmark_service._resolve_max_stored_runs() == 7


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        {"benchmark": "not-an-object"},
        {"benchmark": {"max_stored_runs": 0}},
        {"benchmark": {"max_stored_runs": -3}},
        {"benchmark": {"max_stored_runs": True}},  # bool is an int subclass — rejected
        {"benchmark": {"max_stored_runs": 2.5}},
        {"benchmark": {"max_stored_runs": "20"}},
        {"benchmark": {}},
        {},
    ],
)
def test_config_falls_back_on_bad_values(
    iso_bench: Path, payload: Any
) -> None:
    _write_config(benchmark_service.GLOBAL_CONFIG_PATH, payload)
    assert benchmark_service._resolve_max_stored_runs() == 20


def test_config_falls_back_on_malformed_json(iso_bench: Path) -> None:
    benchmark_service.GLOBAL_CONFIG_PATH.write_text("{ not json", encoding="utf-8")
    assert benchmark_service._resolve_max_stored_runs() == 20


# ---------------------------------------------------------------------------
# Integration — run_benchmark bounds the artifact directory
# ---------------------------------------------------------------------------


class _StubRunner:
    """Returns a prebuilt valid report without touching a model or Docker."""

    async def run_report(self, _problems: Any) -> BenchmarkReport:
        return _valid_report()


def test_run_benchmark_bounds_artifacts(
    iso_bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(benchmark_service.GLOBAL_CONFIG_PATH, {"benchmark": {"max_stored_runs": 3}})
    monkeypatch.setattr(benchmark_service, "_runner_factory", lambda _root: _StubRunner())

    async def _drive() -> None:
        for _ in range(5):
            await benchmark_service.run_benchmark(uuid.uuid4().hex, suite="v1")

    asyncio.run(_drive())

    # Count-only assertion: exactly the cap remains, regardless of mtime resolution.
    assert len(list(iso_bench.glob("*.json"))) == 3
