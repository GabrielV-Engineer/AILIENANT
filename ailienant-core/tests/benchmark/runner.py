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

import time
import uuid
from typing import Awaitable, Callable, List, Optional

from core.token_ledger import token_ledger

from tests.benchmark.arms import AblationArm, apply_arm
from tests.benchmark.hygiene import (
    BenchmarkBudget,
    assert_embeddings_live,
    disable_response_cache,
)
from tests.benchmark.metrics import ProblemMetrics, collect_routing
from tests.benchmark.problems import BenchmarkProblem

# A task runner takes a unique session id and a problem, drives the pipeline, and
# returns when the task is done. Injected so a caller can substitute a stub for
# the model layer without monkeypatching production code.
TaskRunner = Callable[[str, BenchmarkProblem], Awaitable[None]]

_DEFAULT_BUDGET_USD = 25.0


async def _default_task_runner(session_id: str, problem: BenchmarkProblem) -> None:
    """Drive the real pipeline headlessly. Used for live runs, not the gate.

    The pipeline streams only through the broadcast layer, which is inert without
    a connected client. Broadcast isolation for live headless runs is finalized
    alongside the frozen corpus in a later phase.
    """
    from core.task_service import TaskPayload, TaskService

    payload = TaskPayload(
        task_prompt=problem.prompt,
        dirty_buffers=[],
        workspace_root=problem.workspace_root,
    )
    await TaskService().process_task(session_id, payload, "SEQUENTIAL")


class BenchmarkRunner:
    """Runs problems across ablation arms and returns raw metrics."""

    def __init__(
        self,
        task_runner: Optional[TaskRunner] = None,
        budget_usd: float = _DEFAULT_BUDGET_USD,
        telemetry_db_path: Optional[str] = None,
    ) -> None:
        self._run_task: TaskRunner = task_runner or _default_task_runner
        self._budget_usd = budget_usd
        self._telemetry_db_path = telemetry_db_path

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
            await self._run_task(session_id, problem)
        latency = time.perf_counter() - started
        after = token_ledger.snapshot()
        tci, css = collect_routing(session_id)
        budget.check(after)
        return ProblemMetrics(
            arm=arm.value,
            problem_id=problem.problem_id,
            tokens_local=after["local_tokens"] - before["local_tokens"],
            tokens_cloud=after["cloud_tokens"] - before["cloud_tokens"],
            est_usd=after["estimated_invested_usd"] - before["estimated_invested_usd"],
            tci=tci,
            css=css,
            latency_s=latency,
        )

    async def run_arms(
        self, problem: BenchmarkProblem, arms: List[AblationArm]
    ) -> List[ProblemMetrics]:
        """Run one problem across several arms serially and return raw metrics."""
        from core.telemetry import init_telemetry_db

        if self._telemetry_db_path is not None:
            init_telemetry_db(self._telemetry_db_path)
        else:
            init_telemetry_db()
        await assert_embeddings_live()
        budget = BenchmarkBudget.from_snapshot(self._budget_usd, token_ledger.snapshot())
        results: List[ProblemMetrics] = []
        for arm in arms:
            results.append(await self.run_problem(problem, arm, budget))
        return results
