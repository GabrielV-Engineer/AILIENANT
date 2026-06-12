# ailienant-core/tests/test_mcp_dispatch_guard.py
#
# Permission-matrix gate on McpToolAdapter._arun. A mutating remote tool must
# route through evaluate_action before any wire call: denied under plan mode,
# routed to human approval under the default mode (always for the most
# restricted tier), allowed under auto. Read-only tools are friction-free.
# The approval channel is injected so these tests never import the API layer.
#
# 8.4.7 additions: ContextVar ambient injection (gate fires without explicit
# kwargs), trust-once session valve, and default vfs_manager approval channel.

from __future__ import annotations

import contextvars
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_adapter
from tools.mcp_adapter import (
    McpToolAdapter,
    _task_session_id,
    _task_session_mode,
    _grant_session_trust,
    _session_trust,
    clear_session_trust,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(server_name: Optional[str]) -> MagicMock:
    """Register a fake session so an ALLOW verdict reaches a working call_tool."""
    session = MagicMock()
    session.call_tool = AsyncMock(return_value={"ok": True})
    key = server_name or mcp_adapter._DEFAULT_SESSION_KEY
    mcp_adapter._sessions[key] = session
    return session


def _adapter(tool_name: str, *, server_name: Optional[str] = None, description: str = "") -> McpToolAdapter:
    return McpToolAdapter(
        name=tool_name,
        description=description,
        mcp_tool_name=tool_name,
        server_name=server_name,
    )


def _approval(approved: bool) -> AsyncMock:
    return AsyncMock(return_value={"approved": approved})


# ---------------------------------------------------------------------------
# Gate via explicit injected kwargs (8.4.4 baseline)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_catalog_override_makes_query_friction_free() -> None:
    """INTEGRATION PROOF — classification (8.4.1) + catalog (8.4.2) + dispatch (8.4.4).

    A bare ``query`` carries no recognized verb, so the fail-closed heuristic
    would tier it DANGEROUS and force HITL even under the default mode. The
    curated registry overrides ``postgres.query`` to READ_ONLY, so the live
    dispatch calls straight through with no approval. This is the only test that
    exercises all three phases as one coherent chain — if it fails, the
    component-level unit tests do not prove the system works together.
    """
    from core.mcp_registry import init_registry

    init_registry()  # populate the curated tier overrides
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("postgres")
    approve = _approval(True)

    adapter = _adapter("query", server_name="postgres")
    result = await adapter._arun(
        arguments={"sql": "SELECT 1"},
        session_id="s1",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )

    assert result == {"ok": True}
    approve.assert_not_awaited()  # READ_ONLY → no friction
    session.call_tool.assert_awaited_once_with("query", {"sql": "SELECT 1"})


@pytest.mark.anyio
async def test_read_only_under_plan_is_allowed() -> None:
    """A read-only tier short-circuits to ALLOW before the floor/session checks,
    so even plan mode never forces approval on a search-style tool."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("brave-search")
    approve = _approval(True)

    adapter = _adapter("search", server_name="brave-search")
    result = await adapter._arun(
        arguments={"q": "x"},
        session_id="s1",
        session_permission_mode="PLAN",
        request_approval=approve,
    )

    assert result == {"ok": True}
    approve.assert_not_awaited()
    session.call_tool.assert_awaited_once()


@pytest.mark.anyio
async def test_write_under_plan_is_denied() -> None:
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("create_issue", server_name="srv")
    result = await adapter._arun(
        arguments={},
        session_id="s1",
        session_permission_mode="PLAN",
        request_approval=approve,
    )

    assert isinstance(result, str) and "DENIED" in result
    approve.assert_not_awaited()
    session.call_tool.assert_not_awaited()  # never reached the wire


@pytest.mark.anyio
async def test_write_under_default_routes_through_approval() -> None:
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("update_record", server_name="srv")
    result = await adapter._arun(
        arguments={"id": 7},
        session_id="s1",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )

    approve.assert_awaited_once()
    call = approve.await_args
    assert call is not None and call.kwargs["request_kind"] == "MCP_TOOL_CALL"
    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


@pytest.mark.anyio
async def test_write_under_default_blocked_when_rejected() -> None:
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(False)

    adapter = _adapter("write_config", server_name="srv")
    result = await adapter._arun(
        arguments={},
        session_id="s1",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )

    approve.assert_awaited_once()
    assert isinstance(result, str) and "BLOCKED" in result
    session.call_tool.assert_not_awaited()


@pytest.mark.anyio
async def test_dangerous_under_auto_still_requires_approval() -> None:
    """DANGEROUS overrides AUTO — irreversible tools always route to a human."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("delete_branch", server_name="srv")
    result = await adapter._arun(
        arguments={},
        session_id="s1",
        session_permission_mode="AUTO",
        request_approval=approve,
    )

    approve.assert_awaited_once()
    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


@pytest.mark.anyio
async def test_write_under_auto_runs_without_approval() -> None:
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("create_pr", server_name="srv")
    result = await adapter._arun(
        arguments={},
        session_id="s1",
        session_permission_mode="AUTO",
        request_approval=approve,
    )

    approve.assert_not_awaited()
    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


@pytest.mark.anyio
async def test_hitl_without_session_blocks_and_never_calls() -> None:
    """An HITL verdict with no session ID cannot deliver the WS request —
    must refuse rather than silently calling an unapproved tool."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    adapter = _adapter("update_record", server_name="srv")

    # No session_id: cannot route the approval request over WS.
    no_session = await adapter._arun(
        arguments={},
        session_id=None,
        session_permission_mode="DEFAULT",
        request_approval=_approval(True),
    )

    assert "BLOCKED" in no_session
    session.call_tool.assert_not_awaited()


@pytest.mark.anyio
async def test_unwired_caller_falls_through_without_gate() -> None:
    """No session_permission_mode injected AND no ContextVar set → gate is
    skipped entirely (the contract engages only for a graph-wired dispatch)."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    adapter = _adapter("delete_everything", server_name="srv")
    result = await adapter._arun(arguments={"x": 1})

    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# ContextVar ambient injection (8.4.7)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_contextvar_session_wires_gate_without_explicit_kwargs() -> None:
    """When task_service sets the ContextVars before the LangGraph run, the gate
    fires even though LangChain never passes session_permission_mode as a kwarg."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("create_issue", server_name="srv")

    # Simulate what task_service does before the graph run.
    tok_sid = _task_session_id.set("sess-ctx")
    tok_mode = _task_session_mode.set("PLAN")
    try:
        result = await adapter._arun(arguments={}, request_approval=approve)
    finally:
        _task_session_id.reset(tok_sid)
        _task_session_mode.reset(tok_mode)

    # PLAN mode + WRITE tool → DENIED; the wire call never happened.
    assert isinstance(result, str) and "DENIED" in result
    session.call_tool.assert_not_awaited()


@pytest.mark.anyio
async def test_contextvar_default_mode_routes_to_hitl() -> None:
    """ContextVar DEFAULT mode on a WRITE tool routes to the injected approval."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("update_record", server_name="srv")

    tok_sid = _task_session_id.set("sess-ctx2")
    tok_mode = _task_session_mode.set("DEFAULT")
    try:
        result = await adapter._arun(arguments={"id": 1}, request_approval=approve)
    finally:
        _task_session_id.reset(tok_sid)
        _task_session_mode.reset(tok_mode)

    approve.assert_awaited_once()
    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# Trust-once session valve (8.4.7 — DEBT-029 remainder)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trust_once_skips_second_hitl_within_same_session() -> None:
    """After a user approves an MCP WRITE tool, subsequent calls to the same
    tool within the same task session skip HITL entirely."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")
    approve = _approval(True)

    adapter = _adapter("update_record", server_name="srv")

    # First call — HITL fires, user approves, trust is recorded.
    await adapter._arun(
        arguments={"id": 1},
        session_id="s-trust",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )
    approve.assert_awaited_once()

    # Second call — trust valve skips HITL.
    approve2 = _approval(True)
    result2 = await adapter._arun(
        arguments={"id": 2},
        session_id="s-trust",
        session_permission_mode="DEFAULT",
        request_approval=approve2,
    )

    approve2.assert_not_awaited()
    assert result2 == {"ok": True}
    assert session.call_tool.await_count == 2


@pytest.mark.anyio
async def test_trust_is_tool_scoped_not_server_scoped() -> None:
    """Trusting one tool does not grant trust for a different tool on the same server."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    _grant_session_trust("s-scope", "update_record")

    approve = _approval(True)
    adapter_other = _adapter("delete_record", server_name="srv")
    await adapter_other._arun(
        arguments={},
        session_id="s-scope",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )

    # delete_record is DANGEROUS — trust for update_record must not carry over.
    approve.assert_awaited_once()


@pytest.mark.anyio
async def test_clear_session_trust_resets_valve() -> None:
    """clear_session_trust() wipes all grants so HITL fires again after a task ends."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    _grant_session_trust("s-clear", "update_record")
    clear_session_trust("s-clear")

    approve = _approval(True)
    adapter = _adapter("update_record", server_name="srv")
    await adapter._arun(
        arguments={},
        session_id="s-clear",
        session_permission_mode="DEFAULT",
        request_approval=approve,
    )

    # Trust was cleared — HITL must fire again.
    approve.assert_awaited_once()


# ---------------------------------------------------------------------------
# Default vfs_manager approval channel (8.4.7)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_default_approval_channel_fires_from_vfs_manager() -> None:
    """When request_approval is None but session context is present, _arun builds
    a default approval closure from vfs_manager so the gate fires live for
    LangChain-orchestrated calls that never pass the kwarg."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    mock_vfs = MagicMock()
    mock_vfs.request_human_approval = AsyncMock(
        return_value={"approved": True, "comment": None}
    )

    adapter = _adapter("update_record", server_name="srv")

    with patch("tools.mcp_adapter.vfs_manager", mock_vfs, create=True), \
         patch("api.websocket_manager.vfs_manager", mock_vfs):
        result = await adapter._arun(
            arguments={"id": 99},
            session_id="s-dflt",
            session_permission_mode="DEFAULT",
            request_approval=None,   # the live path: no injection
        )

    mock_vfs.request_human_approval.assert_awaited_once()
    call_kwargs = mock_vfs.request_human_approval.await_args.kwargs
    assert call_kwargs["request_kind"] == "MCP_TOOL_CALL"
    assert call_kwargs["session_id"] == "s-dflt"
    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# anyio backend constraint
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
