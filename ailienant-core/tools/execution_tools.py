"""Phase 5.5 — Async Execution Tools (EXECUTE-tier bundle).

Four LangChain BaseTool subclasses + a shared BackgroundTaskManager:

    SandboxBashTool         — Short-lived shell command with HITL friction
                              regex + 2000-char middle-truncation + hard timeout.
    TaskCreateTool          — Spawns a long-lived asyncio subprocess; returns
                              a UUID task_id. Watcher updates the registry.
    TaskGetTool             — Reads back a registered task's status + truncated
                              output. READ_ONLY tier (blueprint §4 line 272).
    CheckTypeIntegrityTool  — Wraps mypy/tsc with the same truncation rules.

Phase 6.2 — HITL Bridge wiring: `sandbox_bash` and `check_type_integrity` no
longer spawn on the host. Their `_arun` bodies route through the process-global
`core.sandbox.ACTIVE_ADAPTER` (resolved at lifespan startup by Phase 6.1.4),
read via `get_active_adapter()`. `task_create` / `BackgroundTaskManager` keep
their native `create_subprocess_shell` path: the blocking `SandboxAdapter`
contract has no fire-and-forget / PID semantics — that routing is deferred
pending an ABC background-execution method.

All remaining subprocess work uses asyncio (create_subprocess_shell + wait_for
+ kill-on-timeout). NEVER subprocess.run or os.system — the FastAPI event loop
MUST NOT block. Pattern mirrored from validators/gates.py.

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
import os
import shlex
import sys
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
from core.sandbox import get_active_adapter
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

# Cooperative-stop escalation window: a cancelled task gets a grace period to
# exit on the soft signal (SIGTERM / TerminateProcess) before it is force-killed
# (SIGKILL / taskkill tree). A process that traps the soft signal can no longer
# survive a stop request and strand its PID.
_STOP_GRACE_S: float = 5.0
_STOP_POLL_INTERVAL_S: float = 0.1

# Execute-tier HITL approvals use a tighter window than the 300 s default: a
# forgotten approval card must not pin the awaiting task (and its session slot)
# for five minutes. On timeout the gate resolves to BLOCKED and never spawns.
_EXEC_HITL_TIMEOUT_SEC: float = 120.0

_EXECUTE_ROLES: FrozenSet[str] = frozenset(
    {"core_dev", "devops_infra", "secops", "qa_tester", "data_ml_engineer"}
)

# sandbox_bash mirrors the roles.py BashTool whitelist specifically (the parity
# matrix gates the shell by exactly the roles granted a shell). The other
# execution tools keep the broader _EXECUTE_ROLES set.
_SANDBOX_BASH_ROLES: FrozenSet[str] = frozenset(
    {"devops_infra", "qa_tester", "vcs_manager", "data_ml_engineer"}
)

# Task V2: orchestrator needs to create and poll background tasks (wave 5 parity).
_TASK_CREATE_ROLES: FrozenSet[str] = _EXECUTE_ROLES | frozenset({"orchestrator"})
_TASK_GET_ROLES: FrozenSet[str] = _EXECUTE_ROLES | frozenset({"orchestrator"})

_SANDBOX_ENV_WHITELIST: Tuple[str, ...] = (
    "PYTHONPATH", "NODE_OPTIONS", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR",
)
"""Env-var NAMES forwarded into the sandbox. PATH is deliberately excluded —
host secrets (API keys, tokens) MUST NOT leak through (PHASE_6_BLUEPRINT.md §2.2)."""

_SANDBOX_UNINITIALIZED_MSG: str = (
    "Sandbox adapter not initialized via lifespan startup."
)


def _sandbox_env() -> Dict[str, str]:
    """Resolve the whitelisted env-var names from the host environment into the
    name→value dict the sandbox adapter expects (ABC ``env_whitelist``)."""
    return {k: os.environ[k] for k in _SANDBOX_ENV_WHITELIST if k in os.environ}


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
    command: str = Field(description="Shell command. Dangerous patterns trigger HITL.")
    timeout_sec: float = Field(
        default=_DEFAULT_BASH_TIMEOUT_SEC, description="Hard timeout (sec)."
    )
    working_dir: Optional[str] = Field(default=None, description="Optional cwd.")
    # session_id / session_permission_mode are caller-injected runtime context,
    # NOT model-chosen arguments — the LLM must never pick its own permission
    # mode. They are accepted by _arun but kept OUT of args_schema so they never
    # enter the tool-selection payload (preserving the Tool-RAG size guarantee).


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
        session_id: Optional[str] = None,
        session_permission_mode: Optional[str] = None,
    ) -> str:
        # Execute-tier permission gate, consulted before any spawn. It engages
        # only when a caller supplies the session policy — the gate is the
        # contract for a graph-wired dispatch that knows the session mode. An
        # unwired caller (no mode) falls through to the dangerous-pattern
        # interceptor, which remains the floor for that path. PLAN denies
        # outright; DEFAULT routes through the HITL card; AUTO falls through.
        if session_permission_mode is not None:
            from core.permissions import (  # deferred — keeps the tool import light
                PermissionDecision,
                gate_execute_action,
                session_mode_from_channel,
            )

            session_mode = session_mode_from_channel(session_permission_mode)
            verdict = gate_execute_action(session_mode)

            if verdict is PermissionDecision.DENY:
                return (
                    "[sandbox_bash] DENIED — plan mode is read-only; "
                    "command not executed."
                )

            if verdict is PermissionDecision.HITL:
                # No session means no channel to surface the card on — refuse
                # rather than silently spawn an unapproved command.
                if not session_id:
                    return (
                        "[sandbox_bash] BLOCKED — command requires HITL approval but "
                        "no session is available to request it; command not executed."
                    )
                from api.websocket_manager import vfs_manager  # deferred — import cycle

                # Await releases the loop until the operator responds or the
                # tighter execute timeout fires; the loop is never busy-spun.
                # Every refusal path returns before get_active_adapter(), so no
                # subprocess is spawned while (or because) we are awaiting.
                approval = await vfs_manager.request_human_approval(
                    session_id=session_id,
                    action_description=f"COMMAND_EXECUTE: {command}",
                    proposed_content=command,
                    request_kind="COMMAND_EXECUTE",
                    timeout_s=_EXEC_HITL_TIMEOUT_SEC,
                )
                if not approval or not approval.get("approved"):
                    return (
                        "[sandbox_bash] BLOCKED — command execution was not "
                        "approved; command not executed."
                    )

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

        # Phase 6.2 — dispatch through the resolved sandbox tier instead of the
        # host. The adapter absorbs the timeout internally (Docker exit 124 /
        # NativeHITL wait_for / Wasm fuel) and always returns a SandboxResult.
        adapter = get_active_adapter()
        if adapter is None:
            raise RuntimeError(_SANDBOX_UNINITIALIZED_MSG)

        result = await adapter.execute(
            command,
            timeout_s=timeout_sec,
            cwd=working_dir or "",
            env_whitelist=_sandbox_env(),
        )
        body = _truncate(result.stdout + result.stderr)
        return f"[sandbox_bash] exit={result.exit_code}\n{body}"


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
        self._procs: Dict[str, asyncio.subprocess.Process] = {}

    async def create(
        self, command: str, working_dir: Optional[str] = None, owner_role: Optional[str] = None
    ) -> str:
        task_id = uuid.uuid4().hex
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        self._procs[task_id] = proc  # stored for stop(); released by _watch on completion
        self._registry[task_id] = {
            "command": command,
            "pid": proc.pid,
            "status": "running",
            "started_at": _now_iso(),
            "completed_at": None,
            "exit_code": None,
            "owner_role": owner_role,
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
        self._procs.pop(task_id, None)  # release proc ref; stop() may have already popped it
        entry = self._registry.get(task_id)
        if entry is None:
            logger.warning("task_watch: task_id=%s vanished from registry", task_id)
            return
        # Race guard: stop() commits "cancelled" before calling terminate(); if we see it
        # here, the cancellation wins — do not overwrite with "completed"/"failed".
        if entry.get("status") == "cancelled":
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

    def list_tasks(self, caller_role: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Snapshot of registered tasks excluding raw output for token hygiene.

        Non-orchestrator callers see only tasks stamped with their own owner_role.
        The orchestrator retains full visibility across all roles. When caller_role
        is None the full snapshot is returned (backward-compatible default).
        """
        _SKIP = frozenset({"truncated_stdout", "truncated_stderr"})
        snapshot = {
            task_id: {k: v for k, v in entry.items() if k not in _SKIP}
            for task_id, entry in self._registry.items()
        }
        if caller_role and caller_role != "orchestrator":
            snapshot = {
                tid: entry for tid, entry in snapshot.items()
                if entry.get("owner_role") == caller_role
            }
        return snapshot

    async def stop(self, task_id: str) -> bool:
        """Terminate a running background process: soft signal, grace, then force-kill.

        Returns False when no running proc is tracked (already completed or unknown).
        Commits 'cancelled' status BEFORE signalling so _watch() respects the race
        guard even if it wakes up first. Sends the soft signal (SIGTERM / Windows
        TerminateProcess), waits up to ``_STOP_GRACE_S`` for the process to exit, then
        escalates to a force-kill (SIGKILL / ``taskkill /T /F`` tree) — a process that
        traps the soft signal can no longer survive a stop and strand its PID. The
        _procs pop is in a finally block to guarantee cleanup regardless of escalation.
        """
        proc = self._procs.get(task_id)
        if proc is None:
            return False
        entry = self._registry.get(task_id)
        if entry:
            entry["status"] = "cancelled"  # committed before signal; _watch respects it
        try:
            try:
                proc.terminate()  # SIGTERM (POSIX) / TerminateProcess (Windows)
            except (ProcessLookupError, OSError):
                return True  # already exited; nothing to escalate

            # Grace period. Poll ``returncode`` rather than awaiting ``proc.wait()``:
            # the _watch task already owns ``proc.communicate()`` (which awaits the
            # same exit), so a second awaiter is avoided.
            waited = 0.0
            while proc.returncode is None and waited < _STOP_GRACE_S:
                await asyncio.sleep(_STOP_POLL_INTERVAL_S)
                waited += _STOP_POLL_INTERVAL_S

            if proc.returncode is None:
                await self._force_kill(proc)
        finally:
            self._procs.pop(task_id, None)  # guaranteed cleanup; prevents zombie reference
        return True

    @staticmethod
    async def _force_kill(proc: asyncio.subprocess.Process) -> None:
        """Force-terminate a process that survived the soft signal + grace window.

        POSIX sends SIGKILL via ``proc.kill()``. Windows shells out to ``taskkill
        /T /F`` (via the non-blocking asyncio subprocess — never ``subprocess.run``)
        to reap the whole process tree, since ``proc.kill()`` is single-PID there.
        """
        if sys.platform == "win32":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except (ProcessLookupError, OSError):
                pass  # process or taskkill already gone
        else:
            try:
                proc.kill()  # SIGKILL
            except (ProcessLookupError, OSError):
                pass


class TaskCreateInput(BaseModel):
    command: str = Field(
        description="Shell command to spawn as a long-running background task."
    )
    working_dir: Optional[str] = Field(
        default=None, description="Optional cwd override for the subprocess."
    )
    owner_role: Optional[str] = Field(
        default=None,
        description=(
            "Role that owns this task. Pass the caller's active_role so task_list "
            "can scope results by role for non-orchestrator callers."
        ),
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

    async def _arun(self, command: str, working_dir: Optional[str] = None, owner_role: Optional[str] = None) -> str:
        task_id = await self._manager.create(command, working_dir=working_dir, owner_role=owner_role)
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
    target_dir: str = Field(description="Target dir.")
    checker: Literal["mypy", "tsc"] = Field(description="Type checker.")


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

        # Phase 6.2 — route through the sandbox tier. The adapter takes a shell
        # string, so the argv tuple is joined; the adapter owns the timeout.
        command = shlex.join(argv)
        adapter = get_active_adapter()
        if adapter is None:
            raise RuntimeError(_SANDBOX_UNINITIALIZED_MSG)

        result = await adapter.execute(
            command,
            timeout_s=_DEFAULT_TYPECHECK_TIMEOUT_SEC,
            cwd="",
            env_whitelist=_sandbox_env(),
        )
        combined = result.stdout + result.stderr
        return (
            f"[check_type_integrity:{checker}] exit={result.exit_code}\n"
            f"{_truncate(combined)}"
        )


# =====================================================================
# Task G — Schema registration
# =====================================================================


def _execute_schema(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    *,
    tier: ToolPrivilegeTier = ToolPrivilegeTier.EXECUTE,
    roles: FrozenSet[str] = _EXECUTE_ROLES,
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=tier,
        allowed_roles=roles,
    )


async def register_execution_tools(store: ToolRAGStore) -> int:
    """Register the 4 execution schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _execute_schema(
            "sandbox_bash",
            "Run a short-lived shell command (2000-char-truncated output, HITL "
            "interceptor on dangerous patterns).",
            SandboxBashInput,
            roles=_SANDBOX_BASH_ROLES,
        ),
        _execute_schema(
            "task_create",
            "Spawn a long-running background asyncio subprocess; returns task_id.",
            TaskCreateInput,
            roles=_TASK_CREATE_ROLES,
        ),
        _execute_schema(
            "task_get",
            "Read status + truncated output of a background task by id.",
            TaskGetInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_TASK_GET_ROLES,
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
