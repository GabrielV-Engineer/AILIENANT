"""Wave 5 gateway/benchmark tool wrappers.

Thin typed BaseTool subclasses that expose the 8.5 benchmark substrate, capability
catalog, skill resolver, and task management surface as RBAC-gated LangChain tools.
They call the same substrate functions as the external gateway verbs — no duplicated
runner logic.

All imports from the substrate (benchmark_service, gateway.catalog, skill_resolver)
are deferred to _arun to keep module-load cheap and avoid circular-import risks.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Set, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.execution_tools import BackgroundTaskManager

logger = logging.getLogger("GATEWAY_TOOLS")


# =====================================================================
# Role sets
# =====================================================================

_BENCHMARK_ROLES: FrozenSet[str] = frozenset({"orchestrator", "qa_tester", "devops_infra"})
_CATALOG_ROLES: FrozenSet[str] = frozenset({"orchestrator", "planner"})
_SKILL_ROLES: FrozenSet[str] = frozenset({"orchestrator", "planner"})
_TASK_MGR_ROLES: FrozenSet[str] = frozenset({"orchestrator"})


# =====================================================================
# GC guard + named cleanup callback for benchmark runner tasks
# =====================================================================

_active_benchmark_tasks: Set["asyncio.Task[None]"] = set()
"""Strong references to in-flight benchmark asyncio.Tasks.

asyncio.create_task() schedules the coroutine but Python's GC may collect the Task
if nothing holds a reference. This set provides that strong reference; the
_cleanup_benchmark done-callback removes the entry once the task finishes or is
cancelled, so there is no permanent leak.
"""


def _cleanup_benchmark(task: "asyncio.Task[None]", suite: str) -> None:
    """Done-callback for benchmark runner tasks.

    Releases the single-flight slot and removes the GC guard entry. Any unhandled
    failure is logged with exc_info so a future reader can reconstruct what failed
    without re-running the benchmark (§5.2 / §12).

    Named function (not a raw lambda) so the logging path can carry exc_info and
    the callback is identifiable in asyncio debug traces.
    """
    _active_benchmark_tasks.discard(task)
    from core import benchmark_service  # deferred — avoids heavy import at load time
    benchmark_service.release_flight()
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Benchmark task for suite %r failed: %s",
            suite,
            task.exception(),
            exc_info=task.exception(),
        )


# Internal agents invoking run_benchmark are billed against a single synthetic
# caller bucket so their compute is accounted for in the same ledger the external
# gateway charges. A distinct id keeps internal spend separable from any external
# caller's budget.
_INTERNAL_CALLER_ID: str = "internal:agent"


def _benchmark_cost() -> float:
    """Flat budget cost charged per benchmark run (env-configurable).

    Reads the same env var as the gateway handler so internal and external runs
    are priced identically, without importing the heavy gateway.handlers module.
    """
    try:
        return float(os.environ.get("AILIENANT_GATEWAY_BENCHMARK_COST", "1.0"))
    except ValueError:
        return 1.0


async def _safe_refund(caller_id: str, amount: float) -> None:
    """Refund a prior charge, never raising — a refund fault must not mask a caller error."""
    from gateway import ledger  # deferred — keeps module-load cheap

    try:
        await ledger.consume_budget(caller_id, -amount)
    except Exception as refund_error:  # noqa: BLE001 — log and move on, never re-raise
        logger.error("benchmark budget refund failed: %s", refund_error)


# =====================================================================
# A — RunBenchmarkTool
# =====================================================================


class RunBenchmarkInput(BaseModel):
    suite: str = Field(
        default="v1",
        description="Benchmark suite identifier. Only 'v1' is currently supported.",
    )


class RunBenchmarkTool(BaseTool):
    """Reserve a single-flight slot and dispatch the benchmark harness.

    Returns a task_id immediately; poll task_get for status, then call
    get_benchmark_report when the run completes.
    """

    name: str = "run_benchmark"
    description: str = (
        "Run the AILIENANT benchmark harness asynchronously. "
        "Returns task_id immediately; poll task_get, then get_benchmark_report. "
        "Responds with 'busy' when another run is already in flight."
    )
    args_schema: Type[BaseModel] = RunBenchmarkInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("RunBenchmarkTool is async-only — use _arun().")

    async def _arun(self, suite: str = "v1") -> str:
        from core import benchmark_service  # deferred — avoids heavy import at load time
        from core.task_service import get_task_service
        from gateway import ledger

        try:
            reserved = benchmark_service.try_reserve(suite)
        except ValueError as exc:
            return f"[run_benchmark] REJECTED: {exc}"

        if not reserved:
            return json.dumps({"status": "busy", "reason": "benchmark_busy"})

        # Pay upfront before dispatch so a crash between dispatch and charge can never
        # hand out a free, expensive run. Refund on any downstream failure. Mirrors
        # the external gateway handler so internal runs are billed identically.
        cost = _benchmark_cost()
        try:
            await ledger.consume_budget(_INTERNAL_CALLER_ID, cost)
        except BaseException:
            benchmark_service.release_flight()
            raise

        task_id = uuid.uuid4().hex
        try:
            runner: asyncio.Task[None] = asyncio.create_task(
                benchmark_service.run_benchmark(task_id, suite),
                name=f"run_benchmark:{task_id}",
            )
        except BaseException:
            # The task never existed, so its done-callback can never fire — release
            # the slot here and refund the upfront charge.
            await _safe_refund(_INTERNAL_CALLER_ID, cost)
            benchmark_service.release_flight()
            raise

        _active_benchmark_tasks.add(runner)
        runner.add_done_callback(functools.partial(_cleanup_benchmark, suite=suite))

        # Register so check_task_status observes the run as in flight; the task's own
        # done-callback auto-deregisters on completion. The benchmark uuid is a
        # distinct key namespace from UI session ids, so this cannot clobber an active
        # generation. On registration failure, cancel the runner (its done-callback
        # performs the single slot release) and refund.
        try:
            get_task_service().register_active_task(task_id, runner)
        except BaseException:
            runner.cancel()
            await _safe_refund(_INTERNAL_CALLER_ID, cost)
            raise

        return json.dumps(
            {
                "task_id": task_id,
                "status": "submitted",
                "poll": "check_task_status",
                "then": "get_benchmark_report",
            }
        )


# =====================================================================
# B — GetBenchmarkReportTool
# =====================================================================


class GetBenchmarkReportInput(BaseModel):
    task_id: str = Field(description="Task ID returned by run_benchmark.")


class GetBenchmarkReportTool(BaseTool):
    """Read the machine-readable report for a completed benchmark run.

    Calls benchmark_service.read_report() via asyncio.to_thread because read_report
    performs synchronous disk I/O (path.read_text). Running it on the event loop
    directly would block all other coroutines on the FastAPI event loop.
    """

    name: str = "get_benchmark_report"
    description: str = (
        "Read the status and report of a benchmark run submitted via run_benchmark. "
        "Returns status='running' while in flight, 'completed' with the full report "
        "when done, or 'failed'/'not_found' for error states."
    )
    args_schema: Type[BaseModel] = GetBenchmarkReportInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GetBenchmarkReportTool is async-only — use _arun().")

    async def _arun(self, task_id: str) -> str:
        from core import benchmark_service  # deferred

        try:
            result = await asyncio.to_thread(benchmark_service.read_report, task_id)
        except (ValueError, FileNotFoundError) as exc:
            # ValueError: malformed or path-traversal task_id rejected by _resolve_artifact.
            # FileNotFoundError: TOCTOU — path.exists() was True but the file vanished
            # before path.read_text() inside the thread (§5.2 host-safety).
            return json.dumps({"status": "rejected", "detail": str(exc)})
        return json.dumps(result)


# =====================================================================
# C — ListCapabilitiesTool
# =====================================================================


class ListCapabilitiesInput(BaseModel):
    include_deprecated: bool = Field(
        default=False,
        description="When true, include capabilities marked as deprecated in the result.",
    )


class ListCapabilitiesTool(BaseTool):
    """List all capabilities exposed by the AILIENANT external gateway."""

    name: str = "list_capabilities"
    description: str = (
        "Return a JSON array of the capabilities the gateway exposes "
        "(name, description, privilege tier, async flag). "
        "Deprecated capabilities are excluded by default."
    )
    args_schema: Type[BaseModel] = ListCapabilitiesInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ListCapabilitiesTool is async-only — use _arun().")

    async def _arun(self, include_deprecated: bool = False) -> str:
        from gateway.catalog import CATALOG  # deferred — mcp package required at runtime

        caps = [
            {
                "name": c.name,
                "description": c.description,
                "tier": c.tier.value,
                "async": c.is_async,
            }
            for c in CATALOG
            if include_deprecated or not c.deprecated
        ]
        return json.dumps(caps)


# =====================================================================
# D — SkillInvokeTool
# =====================================================================


class SkillInvokeInput(BaseModel):
    user_input: str = Field(description="Task or query to match skills against.")
    workspace_root: str = Field(description="Workspace root for scope-filtering skills.")
    skill_id: Optional[str] = Field(
        default=None,
        description="Optional explicit skill ID to invoke directly (bypasses matching).",
    )


class SkillInvokeTool(BaseTool):
    """Resolve and return skills relevant to a task description.

    Passes embed_fn=None — semantic auto-matching is disabled; explicit skill_id
    invocation still works via the DB exact-lookup path. See DEBT-049 for the
    deferred semantic routing with a shared embedder.
    """

    name: str = "skill_invoke"
    description: str = (
        "Resolve skills relevant to the given task. Invoke a specific skill by "
        "skill_id, or omit skill_id for auto-matching (semantic matching is "
        "currently disabled — see DEBT-049). Returns a JSON list capped at 20."
    )
    args_schema: Type[BaseModel] = SkillInvokeInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SkillInvokeTool is async-only — use _arun().")

    async def _arun(
        self, user_input: str, workspace_root: str, skill_id: Optional[str] = None
    ) -> str:
        if not workspace_root.strip():
            return "[skill_invoke] REJECTED: workspace_root is required"

        from core.skill_resolver import resolve_active_skills  # deferred

        skills = await resolve_active_skills(
            user_input=user_input,
            workspace_root=workspace_root,
            invoked_skill_id=skill_id,
            embed_fn=None,  # semantic auto-match deferred; see DEBT-049
        )
        skills_capped = skills[:20]
        return json.dumps({"count": len(skills), "skills": skills_capped})


# =====================================================================
# E — TaskListTool
# =====================================================================


class TaskListInput(BaseModel):
    status_filter: Optional[str] = Field(
        default=None,
        description=(
            "Filter by task status: 'running', 'completed', 'failed', 'cancelled'. "
            "Omit to return all tasks."
        ),
    )


class TaskListTool(BaseTool):
    """List background tasks registered with the BackgroundTaskManager.

    Returns a snapshot of task metadata; raw stdout/stderr output is excluded for
    token hygiene (§5.5). Capped at 50 entries.
    """

    name: str = "task_list"
    description: str = (
        "List all background tasks (spawned via task_create) with their status and "
        "metadata. Raw output is excluded. Results are capped at 50 entries."
    )
    args_schema: Type[BaseModel] = TaskListInput

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskListTool is async-only — use _arun().")

    async def _arun(self, status_filter: Optional[str] = None) -> str:
        tasks = self._manager.list_tasks()
        if status_filter is not None:
            tasks = {k: v for k, v in tasks.items() if v.get("status") == status_filter}
        total = len(tasks)
        capped = dict(list(tasks.items())[:50])
        return json.dumps({"count": total, "tasks": capped, "truncated": total > 50})


# =====================================================================
# F — TaskStopTool
# =====================================================================


class TaskStopInput(BaseModel):
    task_id: str = Field(description="UUID returned by a prior task_create call.")


class TaskStopTool(BaseTool):
    """Terminate a running background task (soft signal, grace, then force-kill).

    Returns 'cancelled' if found and terminated, 'not_found_or_completed' if the
    task was already done or the ID is unknown.
    """

    name: str = "task_stop"
    description: str = (
        "Terminate a background task spawned via task_create (SIGTERM, then SIGKILL "
        "after a grace period). Returns 'cancelled' on success or "
        "'not_found_or_completed' if already done."
    )
    args_schema: Type[BaseModel] = TaskStopInput

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskStopTool is async-only — use _arun().")

    async def _arun(self, task_id: str) -> str:
        stopped = await self._manager.stop(task_id)
        if not stopped:
            return json.dumps({"status": "not_found_or_completed", "task_id": task_id})
        return json.dumps({"status": "cancelled", "task_id": task_id})


# =====================================================================
# Schema registration
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    *,
    tier: ToolPrivilegeTier,
    roles: FrozenSet[str],
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=tier,
        allowed_roles=roles,
    )


async def register_gateway_tools(store: ToolRAGStore) -> int:
    """Register the 6 gateway/benchmark schemas. Returns count (6)."""
    schemas: List[ToolSchema] = [
        _tool_schema(
            "run_benchmark",
            "Run the benchmark harness asynchronously; returns task_id for polling.",
            RunBenchmarkInput,
            tier=ToolPrivilegeTier.EXECUTE,
            roles=_BENCHMARK_ROLES,
        ),
        _tool_schema(
            "get_benchmark_report",
            "Read status + report of a benchmark run by task_id (READ_ONLY poll).",
            GetBenchmarkReportInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_BENCHMARK_ROLES,
        ),
        _tool_schema(
            "list_capabilities",
            "Return JSON array of gateway capabilities (name, description, tier, async).",
            ListCapabilitiesInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_CATALOG_ROLES,
        ),
        _tool_schema(
            "skill_invoke",
            "Resolve skills for a task by explicit ID or auto-match; returns list capped at 20.",
            SkillInvokeInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_SKILL_ROLES,
        ),
        _tool_schema(
            "task_list",
            "List background tasks (status + metadata, no raw output). Cap 50 entries.",
            TaskListInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_TASK_MGR_ROLES,
        ),
        _tool_schema(
            "task_stop",
            "Send SIGTERM to a running background task; returns cancellation status.",
            TaskStopInput,
            tier=ToolPrivilegeTier.EXECUTE,
            roles=_TASK_MGR_ROLES,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
