"""Host-side benchmark execution and report store for the capability gateway.

The gateway's ``run_benchmark`` verb triggers a benchmark here and ``get_report``
reads the result back. A run is identified by a task id; its machine-readable
report is written atomically to a per-run artifact file under the app-data
directory, which is the durable completion signal a reader checks first.

Two safety controls bound the surface an external caller can reach:

* every task id and suite name is validated before it touches the filesystem, so
  a crafted id or suite cannot escape the artifact directory or the corpus set, and
* a single-flight cap bounds how many model- and sandbox-heavy runs execute at once.

The benchmark harness lives under the test tree; it is imported lazily so importing
this module stays cheap and free of heavy backend dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict

if TYPE_CHECKING:
    from core.benchmark.runner import BenchmarkRunner

logger = logging.getLogger("BENCHMARK_SERVICE")

# Co-located with the host run-state under the user's app-data directory.
BENCHMARK_DIR: Path = Path.home() / ".ailienant" / "benchmark"

# The only frozen corpus the harness ships. A suite outside this allowlist is
# rejected before any path is built (fail-closed against a crafted suite name).
_ALLOWED_SUITES = frozenset({"v1"})

# A task id is a uuid4 hex digest and nothing else may name an artifact file. The
# pattern forbids separators and dots, so no id can traverse out of the directory.
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# In-memory transient state. The artifact file is the durable "completed" signal;
# this map only carries "running" and "failed:<detail>" until a host restart.
_runs: Dict[str, str] = {}
_inflight: int = 0


def _max_concurrent() -> int:
    """The single-flight ceiling; defaults to one, never below one."""
    try:
        return max(1, int(os.environ.get("AILIENANT_GATEWAY_BENCH_CONCURRENCY", "1")))
    except ValueError:
        return 1


def _resolve_artifact(task_id: str) -> Path:
    """Map a task id to its artifact path, rejecting anything that could escape.

    The id must be a uuid4 hex digest (no separators, no dots), and the resolved
    path must stay inside ``BENCHMARK_DIR``. Either check failing raises ``ValueError``.
    """
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    path = (BENCHMARK_DIR / f"{task_id}.json").resolve()
    if not path.is_relative_to(BENCHMARK_DIR.resolve()):
        raise ValueError(f"task_id escapes artifact dir: {task_id!r}")
    return path


def _default_runner_factory(corpus_root: Path) -> "BenchmarkRunner":
    """Build a live benchmark runner backed by the Docker sandbox oracle."""
    from core.benchmark.executors import SandboxCodegenExecutor
    from core.benchmark.oracle import BenchmarkOracle
    from core.benchmark.runner import BenchmarkRunner

    return BenchmarkRunner(
        oracle=BenchmarkOracle(corpus_root, SandboxCodegenExecutor()),
    )


# Indirection so the hermetic gate can substitute a fast stub for the live runner.
_runner_factory: Callable[[Path], "BenchmarkRunner"] = _default_runner_factory


def try_reserve(suite: str) -> bool:
    """Validate the suite and reserve a single-flight slot.

    Returns ``False`` when a run is already at the concurrency cap. Raises
    ``ValueError`` for a suite outside the allowlist. The check-then-increment is
    synchronous (no ``await`` between), so two racing requests on the event loop
    cannot both reserve the last slot.
    """
    global _inflight
    if suite not in _ALLOWED_SUITES:
        raise ValueError(f"unknown suite: {suite!r}")
    if _inflight >= _max_concurrent():
        return False
    _inflight += 1
    return True


def release_flight() -> None:
    """Release a reserved slot. The sole releaser of a slot; floors at zero."""
    global _inflight
    _inflight = max(0, _inflight - 1)


async def run_benchmark(task_id: str, suite: str = "v1") -> None:
    """Run the benchmark for one task id and write its report atomically.

    Never raises: a failure is recorded in the transient run map (kept until a
    host restart) and the artifact file is simply absent, so a benchmark fault
    cannot crash the host process. The single-flight slot is released by the
    caller's done-callback, not here.
    """
    from core.benchmark.oracle import load_corpus
    from core.benchmark.problems import BenchmarkProblem
    from core.benchmark.report import write_report

    path = _resolve_artifact(task_id)
    _runs[task_id] = "running"
    try:
        corpus_root, corpus_problems = load_corpus(suite)
        problems = [
            BenchmarkProblem.from_corpus(cp, corpus_root) for cp in corpus_problems
        ]
        runner = _runner_factory(corpus_root)
        report = await runner.run_report(problems)
        write_report(report, path)
        # Success: the file is now the durable answer; drop the transient marker.
        _runs.pop(task_id, None)
    except Exception as exc:  # noqa: BLE001 — a benchmark fault must never crash the host
        logger.error("benchmark run %s failed: %s", task_id, exc, exc_info=True)
        # Keep the failure record so read_report can report it (until a restart).
        _runs[task_id] = f"failed:{exc}"


def read_report(task_id: str) -> Dict[str, Any]:
    """Return a run's status and, when complete, its report.

    The artifact file is authoritative: if it exists, the run completed and its
    contents are returned. Otherwise the transient run map distinguishes a run in
    flight, a recorded failure, or an unknown id.
    """
    path = _resolve_artifact(task_id)
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        return {"status": "completed", "task_id": task_id, "report": report}

    state = _runs.get(task_id)
    if state is None:
        return {"status": "not_found", "task_id": task_id}
    if state.startswith("failed:"):
        return {
            "status": "failed",
            "task_id": task_id,
            "detail": state[len("failed:"):],
        }
    return {"status": "running", "task_id": task_id}
