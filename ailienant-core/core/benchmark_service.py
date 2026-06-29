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

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict

from filelock import FileLock, Timeout

if TYPE_CHECKING:
    from core.benchmark.report import BenchmarkReport
    from core.benchmark.runner import BenchmarkRunner

logger = logging.getLogger("BENCHMARK_SERVICE")

# Co-located with the host run-state under the user's app-data directory.
BENCHMARK_DIR: Path = Path.home() / ".ailienant" / "benchmark"

# Host-global retention config lives in the global rules file (the service has no
# project context — it is keyed only by task id and suite).
GLOBAL_CONFIG_PATH: Path = Path.home() / ".ailienant" / ".ailienant.json"

# Retention defaults. The cap bounds the artifact directory; the lock serializes
# write+prune so the count invariant holds under concurrency.
_DEFAULT_MAX_STORED_RUNS: int = 20
_RETENTION_LOCK_TIMEOUT_S: float = 30.0

# In-process serialization of write+prune. An asyncio.Lock (not threading.Lock):
# run_benchmark is a coroutine, so a blocking lock held across the event loop would
# stall it. Cross-process safety comes from the FileLock at _retention_lock_path.
_retention_async_lock: asyncio.Lock = asyncio.Lock()

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


def _retention_lock_path() -> Path:
    """Lock file guarding the prune critical section.

    Resolved at call time (not frozen at import) so a test that rebinds
    ``BENCHMARK_DIR`` is honoured.
    """
    return BENCHMARK_DIR / ".retention.lock"


def _resolve_max_stored_runs() -> int:
    """Read ``benchmark.max_stored_runs`` from the global rules file.

    Returns the configured positive integer, or ``_DEFAULT_MAX_STORED_RUNS`` for any
    missing file, parse error, wrong shape, non-int, ``bool`` (an ``int`` subclass we
    must reject), or non-positive value. Fail-safe: a malformed config never disables
    the cap.
    """
    try:
        with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _DEFAULT_MAX_STORED_RUNS

    if not isinstance(data, dict):
        return _DEFAULT_MAX_STORED_RUNS
    section: Any = data.get("benchmark")
    if not isinstance(section, dict):
        return _DEFAULT_MAX_STORED_RUNS
    value: Any = section.get("max_stored_runs")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _DEFAULT_MAX_STORED_RUNS
    return value


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

    path = _resolve_artifact(task_id)
    _runs[task_id] = "running"
    try:
        corpus_root, corpus_problems = load_corpus(suite)
        problems = [
            BenchmarkProblem.from_corpus(cp, corpus_root) for cp in corpus_problems
        ]
        runner = _runner_factory(corpus_root)
        report = await runner.run_report(problems)
        await _persist_with_retention(report, path)
        # Success: the file is now the durable answer; drop the transient marker.
        _runs.pop(task_id, None)
    except Exception as exc:  # noqa: BLE001 — a benchmark fault must never crash the host
        logger.error("benchmark run %s failed: %s", task_id, exc, exc_info=True)
        # Keep the failure record so read_report can report it (until a restart).
        _runs[task_id] = f"failed:{exc}"


def _write_then_prune(
    report: "BenchmarkReport", path: Path, max_runs: int
) -> None:
    """Write the report, then evict the oldest artifacts beyond the cap.

    Synchronous: runs in a worker thread under the held retention lock so the
    event loop is never blocked on the atomic write or the unlink sweep.
    """
    from core.benchmark.report import prune_artifacts, write_report

    write_report(report, path)
    prune_artifacts(BENCHMARK_DIR, max_runs)


async def _persist_with_retention(report: "BenchmarkReport", path: Path) -> None:
    """Persist a report under the count cap, atomically and loop-safely.

    The write and prune run together under an in-process ``asyncio.Lock`` plus a
    cross-process ``FileLock``, so concurrent runs (in this host or another) cannot
    race the eviction and exceed the cap. All blocking I/O — the lock acquire, the
    atomic write, and the unlink sweep — runs off the event loop via
    ``asyncio.to_thread``.

    Durability-first: if the cross-process lock is held elsewhere past the timeout,
    the report is written WITHOUT pruning. A completed run's report is the durable
    answer and must never be lost to contention on a best-effort cleanup lock.
    """
    from core.benchmark.report import write_report

    max_runs = _resolve_max_stored_runs()
    # The lock file cannot be created in a missing directory, and write_report's own
    # parent mkdir happens only after the lock would already be needed.
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

    async with _retention_async_lock:
        lock = FileLock(str(_retention_lock_path()), timeout=_RETENTION_LOCK_TIMEOUT_S)
        try:
            await asyncio.to_thread(lock.acquire)
        except Timeout:
            logger.warning(
                "benchmark retention lock busy — writing report without prune."
            )
            await asyncio.to_thread(write_report, report, path)
            return
        try:
            await asyncio.to_thread(_write_then_prune, report, path, max_runs)
        finally:
            await asyncio.to_thread(lock.release)


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
