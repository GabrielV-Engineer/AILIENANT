"""In-process benchmark runner.

Drives a problem through the agent pipeline once per ablation arm, in process,
and collects raw per-problem metrics. Token usage is measured as a pure delta of
the global ledger (snapshot before, snapshot after — the ledger is never reset,
so a concurrent task's count is never destroyed). Each iteration runs under a
fresh session id so its telemetry rows are isolated from every other arm and
pass. Arms run serially: the arm patches are process-global, so serial execution
keeps a patched symbol from leaking into another coroutine, and a fixed seed
wants a reproducible order.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

from core.token_ledger import token_ledger

from tests.benchmark.arms import AblationArm, apply_arm
from tests.benchmark.hygiene import (
    BenchmarkBudget,
    assert_embeddings_live,
    disable_response_cache,
)
from tests.benchmark.metrics import BenchmarkMetricError, ProblemMetrics, collect_routing
from tests.benchmark.problems import BenchmarkProblem
from tests.benchmark.routing_study import RoutingStudyTable, build_routing_study

if TYPE_CHECKING:
    from tests.benchmark.oracle import BenchmarkOracle

# A task runner takes a unique session id and a problem, drives the pipeline, and
# returns the candidate patch (path → new content). Injected so callers can
# substitute a stub for the model layer without monkeypatching production code.
TaskRunner = Callable[[str, BenchmarkProblem], Awaitable[Dict[str, str]]]

_DEFAULT_BUDGET_USD = 25.0
_DEFAULT_INDEXER_TIMEOUT_S = 300.0


def _normalize_patch(raw: Dict[str, str], workspace_root: Path) -> Dict[str, str]:
    """Relativize absolute-path keys; drop escaping paths; pass relative keys through.

    Absolute keys that resolve inside workspace_root are converted to POSIX-style
    relative paths. Keys that escape the workspace are silently dropped — the oracle
    will judge the resulting incomplete patch as a failed verdict rather than crashing.
    Already-relative keys are normalised to POSIX separators and passed through.
    """
    normalized: Dict[str, str] = {}
    for key, content in raw.items():
        p = Path(key)
        if p.is_absolute():
            try:
                rel = p.relative_to(workspace_root)
                normalized[rel.as_posix()] = content
            except ValueError:
                pass
        else:
            normalized[p.as_posix()] = content
    return normalized


async def _default_task_runner(session_id: str, problem: BenchmarkProblem) -> Dict[str, str]:
    """Drive the real pipeline via TaskService. Returns an empty patch dict.

    TaskService.process_task does not expose pending_contents. Use
    _graph_task_runner for live ablation runs that need verdict scoring.
    """
    from core.task_service import TaskPayload, TaskService

    payload = TaskPayload(
        task_prompt=problem.prompt,
        dirty_buffers=[],
        workspace_root=problem.workspace_root,
    )
    await TaskService().process_task(session_id, payload, "SEQUENTIAL")
    return {}


async def _graph_task_runner(session_id: str, problem: BenchmarkProblem) -> Dict[str, str]:
    """Headless graph runner for live ablation runs.

    Builds a minimal TaskPayload, invokes the compiled graph directly, drains
    background tasks, and returns the normalized pending_contents patch. This
    runner is live-only and requires a wired model backend; use an injected
    stub for hermetic gate tests.
    """
    import agents.coder
    from brain.cell_dispatcher import NullCellDispatcher
    from brain.engine import alienant_app
    from brain.state import AIlienantGraphState
    from core.task_service import TaskPayload, TaskService
    from langchain_core.runnables import RunnableConfig
    from typing import cast

    payload = TaskPayload(
        task_prompt=problem.prompt,
        dirty_buffers=[],
        project_id=problem.project_id or "",
        workspace_root=problem.workspace_root,
    )

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    svc = TaskService()
    _state = svc._build_initial_state(session_id, payload, "SEQUENTIAL")
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": session_id,
            "narrate": _noop,
            "stream_thinking": _noop,
            "cell_dispatcher": NullCellDispatcher(),
        }
    }
    final_state: Dict[str, Any] = await alienant_app.ainvoke(
        cast(AIlienantGraphState, _state), config=cfg
    )
    pending: Dict[str, str] = final_state.get("pending_contents") or {}

    # Drain fire-and-forget background tasks. Snapshot before awaiting to avoid
    # RuntimeError from done-callback mutations to the set during iteration.
    tasks = list(agents.coder._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    agents.coder._background_tasks.clear()

    workspace = Path(problem.workspace_root)
    return _normalize_patch(pending, workspace)


class BenchmarkRunner:
    """Runs problems across ablation arms and returns raw metrics."""

    def __init__(
        self,
        task_runner: Optional[TaskRunner] = None,
        budget_usd: float = _DEFAULT_BUDGET_USD,
        telemetry_db_path: Optional[str] = None,
        oracle: Optional["BenchmarkOracle"] = None,
    ) -> None:
        self._run_task: TaskRunner = task_runner or _graph_task_runner
        self._budget_usd = budget_usd
        self._telemetry_db_path = telemetry_db_path
        self._oracle = oracle

    async def run_problem(
        self,
        problem: BenchmarkProblem,
        arm: AblationArm,
        budget: BenchmarkBudget,
    ) -> ProblemMetrics:
        """Run one problem under one arm and return its raw metrics."""
        session_id = uuid.uuid4().hex
        disable_response_cache()
        before = token_ledger.snapshot()
        started = time.perf_counter()
        with apply_arm(arm):
            candidate_patch = await self._run_task(session_id, problem)
        latency = time.perf_counter() - started
        after = token_ledger.snapshot()
        tci, css = collect_routing(session_id)
        budget.check(after)

        verdict: Optional[str] = None
        if problem.corpus_problem is not None and self._oracle is not None:
            if candidate_patch:
                verdict_obj = await self._oracle.run_oracle(
                    problem.corpus_problem, candidate_patch
                )
                verdict = "passed" if verdict_obj.passed else "failed"
            else:
                verdict = "failed"

        return ProblemMetrics(
            arm=arm.value,
            problem_id=problem.problem_id,
            tokens_local=after["local_tokens"] - before["local_tokens"],
            tokens_cloud=after["cloud_tokens"] - before["cloud_tokens"],
            est_usd=after["estimated_invested_usd"] - before["estimated_invested_usd"],
            tci=tci,
            css=css,
            latency_s=latency,
            verdict=verdict,
        )

    async def _prepare_run(self, problem: BenchmarkProblem) -> BenchmarkBudget:
        """Initialize telemetry, verify embeddings, index the corpus once, open a budget.

        The corpus is indexed a single time so every arm and every problem of a
        sweep shares the same graph. The project_id used for indexing must equal
        the one on the payload so graph/vector lookups are keyed correctly. The
        returned budget tracks cumulative spend from this point so a multi-problem
        sweep aborts cleanly once its ceiling is reached.
        """
        from core.telemetry import init_telemetry_db

        if self._telemetry_db_path is not None:
            init_telemetry_db(self._telemetry_db_path)
        else:
            init_telemetry_db()
        await assert_embeddings_live()

        if problem.corpus_root is not None and problem.project_id is not None:
            from core.indexer import LazyIndexer
            from tests.benchmark.oracle import (
                _assert_dependents_nonempty,
                _await_index,
            )

            indexer = LazyIndexer()
            await _await_index(
                problem.corpus_root,
                problem.project_id,
                indexer,
                _DEFAULT_INDEXER_TIMEOUT_S,
            )
            if problem.corpus_problem is not None:
                await _assert_dependents_nonempty(
                    problem.corpus_problem.dependency_seed, problem.project_id
                )

        return BenchmarkBudget.from_snapshot(
            self._budget_usd, token_ledger.snapshot()
        )

    async def run_arms(
        self, problem: BenchmarkProblem, arms: List[AblationArm]
    ) -> List[ProblemMetrics]:
        """Run one problem across several arms serially and return raw metrics.

        The corpus is indexed once before the arm loop so all arms share the same
        indexed graph (index once, measure all).
        """
        budget = await self._prepare_run(problem)
        results: List[ProblemMetrics] = []
        for arm in arms:
            results.append(await self.run_problem(problem, arm, budget))
        return results

    async def run_study(
        self,
        problems: List[BenchmarkProblem],
        *,
        routing_arm: AblationArm = AblationArm.G4,
        baseline_arm: AblationArm = AblationArm.G4_FORCE_CLOUD,
    ) -> RoutingStudyTable:
        """Sweep problems across the routing and baseline arms; return the study table.

        The corpus is indexed once for the whole sweep and a single budget is
        threaded across every problem-arm run so the cumulative ceiling holds.
        Each problem runs both arms serially (the arm patches are process-global),
        and the flat metric list is stratified into a TCI-bucket table.
        """
        if not problems:
            raise BenchmarkMetricError("run_study requires at least one problem")

        budget = await self._prepare_run(problems[0])
        arms = [routing_arm, baseline_arm]
        metrics: List[ProblemMetrics] = []
        for problem in problems:
            for arm in arms:
                metrics.append(await self.run_problem(problem, arm, budget))

        return build_routing_study(
            metrics,
            routing_arm=routing_arm.value,
            baseline_arm=baseline_arm.value,
        )
