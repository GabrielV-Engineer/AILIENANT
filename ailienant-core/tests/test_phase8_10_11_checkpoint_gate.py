"""Checkpoint gate for mutating-tier tool-dispatch HITL routing.

Group 1 (substrate): ToolDispatcher.dispatch routes a HITL-tier call through the
  injectable approval seam — approve runs, deny/absent/raise all degrade to
  deny-with-report without crashing the turn.
Group 2 (agentic cell): run_terminal honors the three-axis permission matrix —
  PLAN denies, AUTO admits, DEFAULT routes EXECUTE through the approval card.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from langchain_core.tools import BaseTool

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall as CellToolCall, run_agentic_cell_node
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
from core.workspace_sync import SyncSurface, _raw_sha256
from shared.rbac import PermissionMode


# ══════════════════════════════════════════════════════════════════════════════
# Group 1 — substrate HITL approval seam
# ══════════════════════════════════════════════════════════════════════════════


class _EchoTool(BaseTool):
    name: str = "echo"
    description: str = "Echo the value back."

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _arun(self, value: str = "x") -> str:
        return f"echo:{value}"


def _reg(tier: ToolPrivilegeTier, roles: set[str]) -> RegisteredTool:
    return RegisteredTool(tool=_EchoTool(), tier=tier, allowed_roles=frozenset(roles))


def _hitl_dispatcher(
    *,
    approval_fn: Any = None,
    tier: ToolPrivilegeTier = ToolPrivilegeTier.EXECUTE,
    session_mode: SessionPermissionMode = SessionPermissionMode.DEFAULT,
) -> ToolDispatcher:
    # EDIT_EXECUTE_RBW is required: any other identity resolves a mutating tier to
    # DENY (not HITL), so the approval seam would never be reached.
    return ToolDispatcher(
        {"echo": _reg(tier, {"core_dev"})},
        active_role="core_dev",
        session_mode=session_mode,
        state={},
        agent_permission=PermissionMode.EDIT_EXECUTE_RBW,
        approval_fn=approval_fn,
    )


def test_hitl_approved_executes() -> None:
    approve = AsyncMock(return_value=True)
    disp = _hitl_dispatcher(approval_fn=approve)
    result = asyncio.run(disp.dispatch(ToolCall(name="echo", args={"value": "hi"})))
    assert result.executed is True
    assert result.observation == "echo:hi"
    approve.assert_awaited_once()


def test_hitl_denied_not_executed() -> None:
    deny = AsyncMock(return_value=False)
    disp = _hitl_dispatcher(approval_fn=deny)
    result = asyncio.run(disp.dispatch(ToolCall(name="echo", args={"value": "hi"})))
    assert result.executed is False
    assert "not approved" in result.observation
    deny.assert_awaited_once()


def test_hitl_no_approval_fn_denies() -> None:
    disp = _hitl_dispatcher(approval_fn=None)
    result = asyncio.run(disp.dispatch(ToolCall(name="echo", args={"value": "hi"})))
    assert result.executed is False
    assert "no approval channel" in result.observation


def test_hitl_approval_raises_degrades() -> None:
    async def _boom(_call: ToolCall, _reg: RegisteredTool) -> bool:
        raise RuntimeError("approval transport down")

    disp = _hitl_dispatcher(approval_fn=_boom)
    result = asyncio.run(disp.dispatch(ToolCall(name="echo", args={"value": "hi"})))
    assert result.executed is False
    assert "approval channel failed" in result.observation


def test_dangerous_tier_routes_to_approval() -> None:
    # DANGEROUS resolves to HITL in ANY non-PLAN mode (here AUTO), so the seam fires.
    approve = AsyncMock(return_value=True)
    disp = _hitl_dispatcher(
        approval_fn=approve,
        tier=ToolPrivilegeTier.DANGEROUS,
        session_mode=SessionPermissionMode.AUTO,
    )
    result = asyncio.run(disp.dispatch(ToolCall(name="echo", args={"value": "hi"})))
    assert result.executed is True
    approve.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# Group 2 — agentic-cell run_terminal HITL routing
# ══════════════════════════════════════════════════════════════════════════════


class _StubSyncSurface(SyncSurface):
    def __init__(self) -> None:
        self._files: Dict[str, bytes] = {}

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}


class _StubSession:
    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        self._q: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._closed = False

    async def run(self, _cmd: str, *, timeout_s: float) -> int:
        await self._q.put(b"ok")
        return self._exit_code

    async def stream(self) -> Any:
        while not self._closed:
            try:
                yield await asyncio.wait_for(self._q.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

    async def write_stdin(self, data: bytes) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def kill(self) -> None:
        self._closed = True

    async def close(self) -> None:
        self._closed = True


class _StubAdapter:
    supports_sessions = True

    def __init__(self, session: _StubSession, surface: _StubSyncSurface) -> None:
        self._session = session
        self._surface = surface

    async def open_session(self, *, cwd: str, env_whitelist: Dict[str, str], **_: Any) -> _StubSession:
        return self._session

    def get_sync_surface(self, cwd: str) -> _StubSyncSurface:
        return self._surface


def _reasoner_one_terminal(command: str = "pytest -q") -> ac.CellReasoner:
    async def _reason(_messages: Any) -> List[CellToolCall]:
        return [CellToolCall(name="run_terminal", args={"command": command})]

    return _reason


@pytest.fixture(autouse=True)
def _clear_cell_registry() -> Any:
    """The cell session registry is a process-singleton; a deferral leaves a session
    open (terminal=False), so clear it between tests to keep them isolated."""
    ac._session_registry.clear()
    yield
    ac._session_registry.clear()


def _run_cell(
    mode: str, *, state_extra: Optional[Dict[str, Any]] = None, **extra: Any
) -> Dict[str, Any]:
    adapter = _StubAdapter(_StubSession(exit_code=0), _StubSyncSurface())
    configurable: Dict[str, Any] = {
        "cell_adapter": adapter,
        "cell_reasoner": _reasoner_one_terminal(),
    }
    configurable.update(extra)
    state: Dict[str, Any] = {
        "task_id": "hitl-gate",
        "user_input": "fix it",
        "workspace_root": "/work",
        "vfs_buffer": {},
        "agentic_iteration": 0,
        "agentic_trajectory": [],
        "session_permission_mode": mode,
        "mission_spec": None,
        "current_cost_usd": 0.0,
        "max_budget_usd": 100.0,
    }
    if state_extra:
        state.update(state_extra)
    return asyncio.run(run_agentic_cell_node(state, {"configurable": configurable}))


def _record(delta: Dict[str, Any]) -> Dict[str, Any]:
    return (delta.get("agentic_trajectory") or [{}])[0]


def test_cell_default_mode_defers_without_running_or_approving() -> None:
    # DEFAULT → EXECUTE resolves to HITL → the cell DEFERS: it captures the command,
    # runs nothing, and does NOT request approval yet (that happens interrupt-first in
    # the next super-step). The 'continue' status routes the cell back to itself.
    approve = AsyncMock(return_value=True)
    delta = _run_cell("DEFAULT", cell_approval_fn=approve)
    approve.assert_not_awaited()
    assert delta.get("pending_exec_command") == "pytest -q"
    assert _record(delta).get("exit_code") is None
    assert _record(delta).get("status") == "continue"


def test_cell_exec_phase_runs_command_once_on_approval() -> None:
    # Re-entry with pending_exec_command set = the exec-approval phase: approval is the
    # FIRST action, then the command runs exactly once. The reasoner is NOT re-invoked.
    approve = AsyncMock(return_value=True)
    calls = {"n": 0}

    async def _counting(_m: Any) -> List[CellToolCall]:
        calls["n"] += 1
        return [CellToolCall(name="run_terminal", args={"command": "pytest -q"})]

    delta = _run_cell(
        "DEFAULT",
        state_extra={"pending_exec_command": "pytest -q"},
        cell_approval_fn=approve,
        cell_reasoner=_counting,
    )
    approve.assert_awaited_once()
    assert _record(delta).get("exit_code") == 0
    assert delta.get("pending_exec_command") is None
    assert calls["n"] == 0, "the reasoner must NOT run in the exec-approval phase"


def test_cell_exec_phase_denied_blocks_command() -> None:
    deny = AsyncMock(return_value=False)
    delta = _run_cell(
        "DEFAULT", state_extra={"pending_exec_command": "pytest -q"}, cell_approval_fn=deny
    )
    deny.assert_awaited_once()
    assert "EXECUTE_TIER_HITL_DENIED" in (delta.get("security_flags") or [])
    assert _record(delta).get("exit_code") is None  # command never ran
    assert delta.get("pending_exec_command") is None


def test_cell_plan_mode_denies() -> None:
    approve = AsyncMock(return_value=True)
    delta = _run_cell("PLAN", cell_approval_fn=approve)
    approve.assert_not_awaited()  # DENY short-circuits before any defer/approval
    assert "EXECUTE_TIER_DENIED" in (delta.get("security_flags") or [])
    assert _record(delta).get("exit_code") is None


def test_cell_auto_mode_runs_without_approval() -> None:
    # No cell_approval_fn injected — AUTO must not require one (regression guard for
    # the existing 7.19 cell suites, which all run under AUTO): runs inline, one shot.
    delta = _run_cell("AUTO")
    assert _record(delta).get("exit_code") == 0
    flags = delta.get("security_flags") or []
    assert "EXECUTE_TIER_HITL_DENIED" not in flags
    assert "EXECUTE_TIER_DENIED" not in flags
