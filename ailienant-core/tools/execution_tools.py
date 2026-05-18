"""Phase 5.5 — Async Execution Tools (EXECUTE-tier bundle).

Four LangChain BaseTool subclasses + a shared BackgroundTaskManager:

    SandboxBashTool         — Short-lived shell command with HITL friction
                              regex + 2000-char middle-truncation + hard timeout.
    TaskCreateTool          — Spawns a long-lived asyncio subprocess; returns
                              a UUID task_id. Watcher updates the registry.
    TaskGetTool             — Reads back a registered task's status + truncated
                              output. READ_ONLY tier (blueprint §4 line 272).
    CheckTypeIntegrityTool  — Wraps mypy/tsc with the same truncation rules.

All subprocess work uses asyncio (create_subprocess_shell / create_subprocess_exec
+ asyncio.wait_for + kill-on-timeout). NEVER subprocess.run or os.system — the
FastAPI event loop MUST NOT block. Pattern mirrored from validators/gates.py.

register_execution_tools(store) registers all four schemas: 3 EXECUTE-tier
(sandbox_bash, task_create, check_type_integrity) + 1 READ_ONLY (task_get).
core/permissions.py is NEVER modified by this module; the enum is only imported.

DANGEROUS_COMMANDS_REGEX is imported from tools.control_tools — that's where the
canonical attack-pattern list lives so the Frontend friction modal can share it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Type,
)

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.control_tools import DANGEROUS_COMMANDS_REGEX

logger = logging.getLogger("EXECUTION_TOOLS")


# =====================================================================
# Shared constants & helpers
# =====================================================================

TASK_OUTPUT_TRUNC: int = 2000
"""Canonical truncation cap (chars). PHASE_5_BLUEPRINT.md §5.1 line 293."""

_DEFAULT_BASH_TIMEOUT_SEC: float = 30.0
_DEFAULT_TYPECHECK_TIMEOUT_SEC: float = 120.0

_EXECUTE_ROLES: FrozenSet[str] = frozenset(
    {"core_dev", "devops_infra", "secops", "qa_tester", "data_ml_engineer"}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str) -> str:
    """Middle-truncate text to TASK_OUTPUT_TRUNC chars.

    For text under the cap, returns as-is. For larger text, keeps the first
    and last halves and injects a `[TRUNCATED {N} BYTES]` marker in the middle.
    """
    n = len(text)
    if n <= TASK_OUTPUT_TRUNC:
        return text
    head = TASK_OUTPUT_TRUNC // 2
    tail = TASK_OUTPUT_TRUNC - head
    return (
        text[:head]
        + f"\n...[TRUNCATED {n - TASK_OUTPUT_TRUNC} BYTES]...\n"
        + text[-tail:]
    )


def _match_dangerous(command: str) -> Optional[str]:
    """Return the matching pattern's source if any DANGEROUS_COMMANDS_REGEX hits.

    None if the command is safe. The pattern's source is included in the
    interceptor's return string so the agent has a clear hint of what tripped.
    """
    for pat in DANGEROUS_COMMANDS_REGEX:
        if pat.search(command):
            return pat.pattern
    return None


# =====================================================================
# Task A — SandboxBashTool
# =====================================================================


class SandboxBashInput(BaseModel):
    command: str = Field(
        description=(
            "Shell command to execute. Subject to DANGEROUS_COMMANDS_REGEX "
            "pre-check; matches block the spawn and request HITL approval."
        )
    )
    timeout_sec: float = Field(
        default=_DEFAULT_BASH_TIMEOUT_SEC,
        description="Hard timeout; process is killed on overrun.",
    )
    working_dir: Optional[str] = Field(
        default=None, description="Optional cwd override."
    )


class SandboxBashTool(BaseTool):
    """Short-lived async shell command with HITL friction + truncation.

    Output (stdout+stderr) is capped at TASK_OUTPUT_TRUNC chars via middle
    truncation. Commands matching DANGEROUS_COMMANDS_REGEX are intercepted
    and routed to a HITL approval flow (the agent must call
    ask_user_question and then retry after explicit operator consent).
    """

    name: str = "sandbox_bash"
    description: str = (
        "Run a short-lived shell command inside the sandboxed event loop. "
        "Output is capped at 2000 chars with middle truncation. Commands "
        "matching the dangerous-pattern list (rm -rf, sudo, drop table, ...) "
        "are intercepted and routed to a HITL approval flow."
    )
    args_schema: Type[BaseModel] = SandboxBashInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SandboxBashTool is async-only — use _arun().")

    async def _arun(
        self,
        command: str,
        timeout_sec: float = _DEFAULT_BASH_TIMEOUT_SEC,
        working_dir: Optional[str] = None,
    ) -> str:
        pattern = _match_dangerous(command)
        if pattern is not None:
            logger.warning(
                "sandbox_bash: blocked DANGEROUS command (pattern=%r): %s",
                pattern,
                command,
            )
            return (
                f"[sandbox_bash] DANGEROUS_COMMAND_INTERCEPTED — pattern {pattern!r} "
                f"matched. Use ask_user_question to request HITL approval before retrying."
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
        except FileNotFoundError as exc:
            return f"[sandbox_bash] SPAWN_ERROR: {exc}"

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[sandbox_bash] TIMEOUT after {timeout_sec}s"

        combined = (
            stdout_bytes.decode("utf-8", errors="replace")
            + stderr_bytes.decode("utf-8", errors="replace")
        )
        body = _truncate(combined)
        return f"[sandbox_bash] exit={proc.returncode}\n{body}"


# =====================================================================
# Task B — BackgroundTaskManager + TaskCreateTool + TaskGetTool
# =====================================================================


class BackgroundTaskManager:
    """Owns the lifecycle of long-running asyncio.subprocess.Process handles.

    The registry dict is provided by the caller (typically a reference to
    state['background_tasks']) so mutations are visible to the orchestrator
    without any explicit reducer wiring. Watcher tasks are held in a per-
    manager strong-ref set so GC cannot drop them mid-flight (precedent:
    agents/coder.py:101).
    """

    def __init__(self, registry: MutableMapping[str, Dict[str, Any]]) -> None:
        self._registry = registry
        self._tasks: Set[asyncio.Task[None]] = set()

    async def create(
        self, command: str, working_dir: Optional[str] = None
    ) -> str:
        task_id = uuid.uuid4().hex
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        self._registry[task_id] = {
            "command": command,
            "pid": proc.pid,
            "status": "running",
            "started_at": _now_iso(),
            "completed_at": None,
            "exit_code": None,
            "truncated_stdout": "",
            "truncated_stderr": "",
        }
        watcher = asyncio.create_task(self._watch(task_id, proc))
        self._tasks.add(watcher)
        watcher.add_done_callback(self._tasks.discard)
        logger.info("task_create: task_id=%s pid=%d cmd=%r", task_id, proc.pid, command)
        return task_id

    async def _watch(
        self, task_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        stdout_bytes, stderr_bytes = await proc.communicate()
        entry = self._registry.get(task_id)
        if entry is None:
            logger.warning("task_watch: task_id=%s vanished from registry", task_id)
            return
        entry["status"] = "completed" if proc.returncode == 0 else "failed"
        entry["completed_at"] = _now_iso()
        entry["exit_code"] = proc.returncode
        entry["truncated_stdout"] = _truncate(
            stdout_bytes.decode("utf-8", errors="replace")
        )
        entry["truncated_stderr"] = _truncate(
            stderr_bytes.decode("utf-8", errors="replace")
        )

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(task_id)


class TaskCreateInput(BaseModel):
    command: str = Field(
        description="Shell command to spawn as a long-running background task."
    )
    working_dir: Optional[str] = Field(
        default=None, description="Optional cwd override for the subprocess."
    )


class TaskCreateTool(BaseTool):
    """Spawn a long-running asyncio subprocess; return its UUID task_id.

    No HITL friction check applies here (the agent explicitly opted into a
    background task; per-job friction is a Phase 5.7 feature). State mutation
    happens inside the injected BackgroundTaskManager — the registry is a
    reference to state['background_tasks'] supplied at factory time.
    """

    name: str = "task_create"
    description: str = (
        "Spawn a long-running shell command as a background asyncio task. "
        "Returns a UUID task_id; status and truncated output are polled "
        "via task_get."
    )
    args_schema: Type[BaseModel] = TaskCreateInput

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskCreateTool is async-only — use _arun().")

    async def _arun(self, command: str, working_dir: Optional[str] = None) -> str:
        task_id = await self._manager.create(command, working_dir=working_dir)
        return f"[task_create] OK task_id={task_id}"


class TaskGetInput(BaseModel):
    task_id: str = Field(description="UUID returned by a prior task_create call.")


class TaskGetTool(BaseTool):
    """Read back the truncated status + output of a registered background task.

    READ_ONLY tier (PHASE_5_BLUEPRINT.md §4 line 272) — the tool only reads the
    in-memory registry and never spawns processes. Marking it EXECUTE would
    block status polling from PLAN mode, which would defeat self-monitoring.
    """

    name: str = "task_get"
    description: str = (
        "Read the status (running / completed / failed) and 2000-char-truncated "
        "stdout+stderr of a background task spawned via task_create."
    )
    args_schema: Type[BaseModel] = TaskGetInput

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskGetTool is async-only — use _arun().")

    async def _arun(self, task_id: str) -> str:
        entry = self._manager.get(task_id)
        if entry is None:
            return f"[task_get] UNKNOWN task_id={task_id}"
        return (
            f"[task_get] {task_id} "
            f"status={entry['status']} exit={entry['exit_code']}\n"
            f"stdout:\n{entry['truncated_stdout']}\n"
            f"stderr:\n{entry['truncated_stderr']}"
        )


# =====================================================================
# Task C — CheckTypeIntegrityTool
# =====================================================================


class CheckTypeIntegrityInput(BaseModel):
    target_dir: str = Field(description="Directory passed to the type checker.")
    checker: Literal["mypy", "tsc"] = Field(
        description="Which type checker to invoke."
    )


class CheckTypeIntegrityTool(BaseTool):
    """Run mypy (Python) or tsc (TypeScript) against a target dir; truncate output."""

    name: str = "check_type_integrity"
    description: str = (
        "Run mypy --strict (Python) or tsc --noEmit -p (TypeScript) against "
        "the target directory; output is 2000-char-truncated."
    )
    args_schema: Type[BaseModel] = CheckTypeIntegrityInput

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "CheckTypeIntegrityTool is async-only — use _arun()."
        )

    async def _arun(self, target_dir: str, checker: Literal["mypy", "tsc"]) -> str:
        argv: Tuple[str, ...]
        if checker == "mypy":
            argv = ("python", "-m", "mypy", "--strict", target_dir)
        else:
            argv = ("npx", "--no-install", "tsc", "--noEmit", "-p", target_dir)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return f"[check_type_integrity:{checker}] SPAWN_ERROR: {exc}"

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_DEFAULT_TYPECHECK_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                f"[check_type_integrity:{checker}] TIMEOUT after "
                f"{_DEFAULT_TYPECHECK_TIMEOUT_SEC}s"
            )

        combined = (
            stdout_bytes.decode("utf-8", errors="replace")
            + stderr_bytes.decode("utf-8", errors="replace")
        )
        return f"[check_type_integrity:{checker}] exit={proc.returncode}\n{_truncate(combined)}"


# =====================================================================
# Task G — Schema registration
# =====================================================================


def _execute_schema(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    *,
    tier: ToolPrivilegeTier = ToolPrivilegeTier.EXECUTE,
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=tier,
        allowed_roles=_EXECUTE_ROLES,
    )


async def register_execution_tools(store: ToolRAGStore) -> int:
    """Register the 4 execution schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _execute_schema(
            "sandbox_bash",
            "Run a short-lived shell command (2000-char-truncated output, HITL "
            "interceptor on dangerous patterns).",
            SandboxBashInput,
        ),
        _execute_schema(
            "task_create",
            "Spawn a long-running background asyncio subprocess; returns task_id.",
            TaskCreateInput,
        ),
        _execute_schema(
            "task_get",
            "Read status + truncated output of a background task by id.",
            TaskGetInput,
            tier=ToolPrivilegeTier.READ_ONLY,
        ),
        _execute_schema(
            "check_type_integrity",
            "Run mypy or tsc against a target directory; 2000-char-truncated output.",
            CheckTypeIntegrityInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
