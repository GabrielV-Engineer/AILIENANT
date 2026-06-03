"""Execute-tier gate wiring — no execute path bypasses the permission matrix.

The matrix verdicts themselves are exercised in ``test_permissions.py``; here we
lock the *wiring*: the shared helpers map channel strings correctly, and
``SandboxBashTool._arun`` consults the gate before any spawn — denying under
PLAN, requiring an approval card under DEFAULT (with the right request_kind and
a tightened timeout so a forgotten card can't pin the loop), and refusing to
spawn when no session is available to ask on.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.audit import _classify
from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    gate_execute_action,
    session_mode_from_channel,
)
from core.sandbox import SandboxResult
from tools.execution_tools import _EXEC_HITL_TIMEOUT_SEC, SandboxBashTool

import tools.execution_tools as exec_mod


# ── Helpers + matrix-wiring ──────────────────────────────────────────────────


def _no_adapter() -> Any:
    raise AssertionError("get_active_adapter must not be called when the gate blocks")


def test_session_mode_from_channel_uppercase_roundtrip() -> None:
    assert session_mode_from_channel("PLAN") is SessionPermissionMode.PLAN
    assert session_mode_from_channel("AUTO") is SessionPermissionMode.AUTO
    assert session_mode_from_channel("DEFAULT") is SessionPermissionMode.DEFAULT


def test_session_mode_from_channel_unknown_defaults_safe() -> None:
    assert session_mode_from_channel(None) is SessionPermissionMode.DEFAULT
    assert session_mode_from_channel("nonsense") is SessionPermissionMode.DEFAULT


def test_gate_execute_verdicts() -> None:
    assert gate_execute_action(SessionPermissionMode.PLAN) is PermissionDecision.DENY
    assert gate_execute_action(SessionPermissionMode.DEFAULT) is PermissionDecision.HITL
    assert gate_execute_action(SessionPermissionMode.AUTO) is PermissionDecision.ALLOW


def test_audit_classifies_command_execute() -> None:
    assert _classify("COMMAND_EXECUTE: ls -la") == "COMMAND_EXECUTE"


# ── _arun gate wiring ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sandbox_bash_plan_mode_denies_without_spawn(monkeypatch: Any) -> None:
    monkeypatch.setattr(exec_mod, "get_active_adapter", _no_adapter)
    out = await SandboxBashTool()._arun(
        command="ls", session_id="s1", session_permission_mode="PLAN"
    )
    assert "DENIED" in out


@pytest.mark.anyio
async def test_sandbox_bash_default_rejected_blocks_without_spawn(monkeypatch: Any) -> None:
    monkeypatch.setattr(exec_mod, "get_active_adapter", _no_adapter)
    approve = AsyncMock(return_value={"approved": False})
    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval", approve
    )
    out = await SandboxBashTool()._arun(
        command="ls", session_id="s1", session_permission_mode="DEFAULT"
    )
    assert "BLOCKED" in out
    approve.assert_awaited_once()
    assert approve.await_args is not None
    kwargs = approve.await_args.kwargs
    assert kwargs.get("request_kind") == "COMMAND_EXECUTE"


@pytest.mark.anyio
async def test_sandbox_bash_default_timeout_uses_tight_bound(monkeypatch: Any) -> None:
    monkeypatch.setattr(exec_mod, "get_active_adapter", _no_adapter)
    approve = AsyncMock(return_value=None)  # None == HITL timeout
    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval", approve
    )
    out = await SandboxBashTool()._arun(
        command="ls", session_id="s1", session_permission_mode="DEFAULT"
    )
    assert "BLOCKED" in out
    assert approve.await_args is not None
    assert approve.await_args.kwargs.get("timeout_s") == _EXEC_HITL_TIMEOUT_SEC


@pytest.mark.anyio
async def test_sandbox_bash_hitl_without_session_refuses(monkeypatch: Any) -> None:
    monkeypatch.setattr(exec_mod, "get_active_adapter", _no_adapter)
    out = await SandboxBashTool()._arun(
        command="ls", session_id=None, session_permission_mode="DEFAULT"
    )
    assert "BLOCKED" in out


@pytest.mark.anyio
async def test_sandbox_bash_auto_executes(monkeypatch: Any) -> None:
    adapter = AsyncMock()
    adapter.execute = AsyncMock(
        return_value=SandboxResult(exit_code=0, stdout="hello\n", stderr="")
    )
    monkeypatch.setattr(exec_mod, "get_active_adapter", lambda: adapter)
    out = await SandboxBashTool()._arun(
        command="echo hello", session_id="s1", session_permission_mode="AUTO"
    )
    assert "exit=0" in out
    assert "hello" in out
    adapter.execute.assert_awaited_once()
