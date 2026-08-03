# ailienant-core/tests/test_execution_tools.py
#
# Phase 5.5 smoke tests for the EXECUTE-tier execution bundle.
# Subprocess pattern: every test uses asyncio.create_subprocess_shell/_exec
# (the production code path) — no synchronous subprocess.run anywhere.

from __future__ import annotations

import asyncio
import hashlib
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import core.sandbox as sandbox_module
from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore
from tools.agent_tools import make_task_create_tool, make_task_get_tool
from tools.execution_tools import (
    TASK_OUTPUT_TRUNC,
    BackgroundTaskManager,
    CheckTypeIntegrityTool,
    SandboxBashTool,
    TaskCreateTool,
    TaskGetTool,
    _EXECUTE_ROLES,
    _SANDBOX_BASH_ROLES,
    _TASK_CREATE_ROLES,
    _TASK_GET_ROLES,
    register_execution_tools,
)

# Captured at collection time, before the conftest autouse fixture monkeypatches
# core.sandbox.get_trusted_adapter_silent to delegate straight to the fake oracle
# — the DEBT-086 no-bridge regression test below needs the REAL resolver so it
# actually exercises DevcontainerSandboxAdapter's fallback path, not the test
# shortcut every other test in this suite relies on.
_REAL_GET_TRUSTED_ADAPTER_SILENT = sandbox_module.get_trusted_adapter_silent


# =====================================================================
# SandboxBashTool
# =====================================================================


@pytest.mark.anyio
async def test_sandbox_bash_happy_path() -> None:
    tool = SandboxBashTool()
    out = await tool._arun(command=f'{sys.executable} -c "print(\'hello\')"')
    assert "hello" in out
    assert "exit=0" in out


@pytest.mark.anyio
async def test_sandbox_bash_truncation_caps_output() -> None:
    tool = SandboxBashTool()
    out = await tool._arun(
        command=f'{sys.executable} -c "print(\'x\'*3000)"'
    )
    assert "[TRUNCATED" in out
    # Header line + truncated body should be reasonably bounded.
    assert len(out) <= TASK_OUTPUT_TRUNC + 300


@pytest.mark.anyio
async def test_sandbox_bash_timeout_kills_process() -> None:
    tool = SandboxBashTool()
    out = await tool._arun(
        command=f'{sys.executable} -c "import time; time.sleep(5)"',
        timeout_sec=0.5,
    )
    assert "[sandbox_bash] exit=124" in out  # exit 124 = timeout (GNU timeout / _DirectAdapter)


@pytest.mark.anyio
async def test_sandbox_bash_blocks_dangerous_command() -> None:
    tool = SandboxBashTool()
    spawn_calls: List[Any] = []

    async def _exploding_spawn(*args: Any, **kwargs: Any) -> Any:
        spawn_calls.append((args, kwargs))
        raise AssertionError("Subprocess should NEVER spawn on dangerous match.")

    with patch(
        "tools.execution_tools.asyncio.create_subprocess_shell",
        side_effect=_exploding_spawn,
    ):
        out = await tool._arun(command="rm -rf /tmp/anything")

    assert "DANGEROUS_COMMAND_INTERCEPTED" in out
    assert "rm" in out
    assert spawn_calls == []  # no spawn happened


@pytest.mark.anyio
async def test_sandbox_bash_captures_stderr() -> None:
    tool = SandboxBashTool()
    out = await tool._arun(
        command=(
            f'{sys.executable} -c "import sys; sys.stderr.write(\'boom\')"'
        )
    )
    assert "boom" in out
    assert "exit=0" in out


@pytest.mark.anyio
async def test_sandbox_bash_reports_non_zero_exit() -> None:
    tool = SandboxBashTool()
    out = await tool._arun(
        command=f'{sys.executable} -c "import sys; sys.exit(7)"'
    )
    assert "exit=7" in out


# =====================================================================
# BackgroundTaskManager + TaskCreateTool + TaskGetTool
# =====================================================================


async def _wait_for_status(
    manager: BackgroundTaskManager, task_id: str, expected: str, timeout: float = 5.0
) -> Dict[str, Any]:
    """Poll the registry until the watcher writes the expected status."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        entry = manager.get(task_id)
        if entry is not None and entry.get("status") == expected:
            return entry
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"task {task_id} did not reach status={expected!r} within {timeout}s; "
        f"last entry={manager.get(task_id)!r}"
    )


@pytest.mark.anyio
async def test_task_create_spawns_and_mutates_state() -> None:
    state: Dict[str, Any] = {}
    tool = make_task_create_tool(state)
    out = await tool._arun(command=f'{sys.executable} -c "print(\'hi\')"')
    assert "[task_create] OK task_id=" in out
    task_id = out.rsplit("task_id=", 1)[1].strip()
    assert "background_tasks" in state
    entry = state["background_tasks"][task_id]
    assert entry["command"].endswith("print('hi')\"")
    assert entry["pid"] > 0
    assert entry["status"] in {"running", "completed"}


@pytest.mark.anyio
async def test_task_get_returns_completion_status() -> None:
    state: Dict[str, Any] = {}
    registry: Dict[str, Dict[str, Any]] = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    create_tool = TaskCreateTool(manager=manager)
    get_tool = TaskGetTool(manager=manager)

    create_out = await create_tool._arun(
        command=f'{sys.executable} -c "print(\'hi\')"'
    )
    task_id = create_out.rsplit("task_id=", 1)[1].strip()
    entry = await _wait_for_status(manager, task_id, "completed")
    assert entry["exit_code"] == 0
    assert "hi" in entry["truncated_stdout"]

    out = await get_tool._arun(task_id=task_id)
    assert "status=completed" in out
    assert "exit=0" in out
    assert "hi" in out


@pytest.mark.anyio
async def test_task_failure_status_is_recorded() -> None:
    state: Dict[str, Any] = {}
    registry: Dict[str, Dict[str, Any]] = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    create_tool = TaskCreateTool(manager=manager)

    out = await create_tool._arun(
        command=f'{sys.executable} -c "import sys; sys.exit(2)"'
    )
    task_id = out.rsplit("task_id=", 1)[1].strip()
    entry = await _wait_for_status(manager, task_id, "failed")
    assert entry["exit_code"] == 2


@pytest.mark.anyio
async def test_task_watcher_truncates_large_stdout() -> None:
    state: Dict[str, Any] = {}
    registry: Dict[str, Dict[str, Any]] = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    create_tool = TaskCreateTool(manager=manager)

    out = await create_tool._arun(
        command=f'{sys.executable} -c "print(\'x\'*3000)"'
    )
    task_id = out.rsplit("task_id=", 1)[1].strip()
    entry = await _wait_for_status(manager, task_id, "completed")
    assert "[TRUNCATED" in entry["truncated_stdout"]


@pytest.mark.anyio
async def test_task_get_unknown_id_returns_error_string() -> None:
    state: Dict[str, Any] = {}
    tool = make_task_get_tool(state)
    out = await tool._arun(task_id="0" * 32)
    assert "UNKNOWN task_id=" in out


@pytest.mark.anyio
async def test_create_and_get_share_registry_through_state() -> None:
    """D5: independent managers, same backing dict — get sees create's writes."""
    state: Dict[str, Any] = {}
    create_tool = make_task_create_tool(state)
    get_tool = make_task_get_tool(state)

    out = await create_tool._arun(command=f'{sys.executable} -c "print(\'hi\')"')
    task_id = out.rsplit("task_id=", 1)[1].strip()
    # Even before the watcher finishes, get_tool can read the running entry.
    get_out = await get_tool._arun(task_id=task_id)
    assert task_id in get_out
    assert "UNKNOWN" not in get_out


# =====================================================================
# CheckTypeIntegrityTool
# =====================================================================


@pytest.mark.anyio
async def test_check_type_integrity_mypy_returns_exit_header(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text("x: int = 1\n")
    tool = CheckTypeIntegrityTool()
    out = await tool._arun(target_dir=str(tmp_path), checker="mypy")
    assert out.startswith("[check_type_integrity:mypy] exit=")


def test_check_type_integrity_invalid_checker_rejected_by_pydantic() -> None:
    with pytest.raises(ValidationError):
        CheckTypeIntegrityTool().args_schema(target_dir=".", checker="flake8")


# ── DEBT-086: session-aware tier routing ─────────────────────────────────────


@pytest.mark.anyio
async def test_check_type_integrity_with_session_routes_through_silent_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bound session_id must route through the non-interactive trusted
    resolver only — never the interactive one, which would raise its own
    HITL card on top of whatever gated this call upstream."""
    import core.sandbox as sb

    calls: List[str] = []

    def _silent() -> Any:
        calls.append("silent")
        return sb.get_active_adapter()

    def _interactive() -> Any:
        calls.append("interactive")
        return sb.get_active_adapter()

    monkeypatch.setattr(sb, "get_trusted_adapter_silent", _silent)
    monkeypatch.setattr(sb, "get_trusted_adapter", _interactive)

    tool = CheckTypeIntegrityTool()
    tool._session_id = "s1"  # type: ignore[attr-defined]
    out = await tool._arun(target_dir=str(tmp_path), checker="mypy")

    assert calls == ["silent"], "must route through the non-interactive resolver only"
    assert out.startswith("[check_type_integrity:mypy] exit=")


@pytest.mark.anyio
async def test_check_type_integrity_without_session_never_touches_trusted_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unfactoried instance (no session bound) must be byte-identical to
    pre-DEBT-086 behavior: the oracle tier directly, no trusted resolver
    consulted at all."""
    import core.sandbox as sb

    def _fail() -> object:
        raise AssertionError("trusted resolver must not be consulted without a session_id")

    monkeypatch.setattr(sb, "get_trusted_adapter_silent", _fail)
    monkeypatch.setattr(sb, "get_trusted_adapter", _fail)

    out = await CheckTypeIntegrityTool()._arun(target_dir=str(tmp_path), checker="mypy")
    assert out.startswith("[check_type_integrity:mypy] exit=")


@pytest.mark.anyio
async def test_check_type_integrity_session_no_bridge_degrades_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: a session_id bound but no devcontainer bridge injected
    must degrade straight to the oracle cage — it must NEVER raise its own HITL
    approval card. This is the double-prompt bug DEBT-086 could have
    reintroduced if it had reused get_trusted_adapter (the interactive
    resolver) instead of the silent one."""
    import core.sandbox as sb

    monkeypatch.setattr(sb, "get_trusted_adapter_silent", _REAL_GET_TRUSTED_ADAPTER_SILENT)
    sb.reset_trusted_adapter()  # drop any singleton cached under the conftest fake

    async def _approval_must_not_be_called(*args: object, **kwargs: object) -> Dict[str, object]:
        raise AssertionError(
            "check_type_integrity must never request HITL approval of its own"
        )

    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval",
        _approval_must_not_be_called,
    )

    tool = CheckTypeIntegrityTool()
    tool._session_id = "s1"  # type: ignore[attr-defined]
    out = await tool._arun(target_dir=str(tmp_path), checker="mypy")
    assert out.startswith("[check_type_integrity:mypy] exit=")


# =====================================================================
# register_execution_tools
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


@pytest.mark.anyio
async def test_register_execution_tools_registers_four_schemas(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_execution_tools(store)
    assert count == 4
    names = {s.name for s in store.all_schemas()}
    assert names == {"sandbox_bash", "task_create", "task_get", "check_type_integrity"}


@pytest.mark.anyio
async def test_execution_tier_assignment(tmp_path: Path) -> None:
    """Per D4: task_get is READ_ONLY; the other 3 are EXECUTE."""
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    by_name = {s.name: s for s in store.all_schemas()}
    assert by_name["sandbox_bash"].privilege_tier is ToolPrivilegeTier.EXECUTE
    assert by_name["task_create"].privilege_tier is ToolPrivilegeTier.EXECUTE
    assert by_name["check_type_integrity"].privilege_tier is ToolPrivilegeTier.EXECUTE
    assert by_name["task_get"].privilege_tier is ToolPrivilegeTier.READ_ONLY
    # sandbox_bash mirrors the roles.py BashTool whitelist specifically; the other
    # three execution tools keep the broader _EXECUTE_ROLES set.
    assert by_name["sandbox_bash"].allowed_roles == _SANDBOX_BASH_ROLES
    # Task V2: task_create and task_get are extended to include orchestrator.
    assert by_name["task_create"].allowed_roles == _TASK_CREATE_ROLES
    assert by_name["task_get"].allowed_roles == _TASK_GET_ROLES
    assert by_name["check_type_integrity"].allowed_roles == _EXECUTE_ROLES


# =====================================================================
# anyio backend constraint
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
