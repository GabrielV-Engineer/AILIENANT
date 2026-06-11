# ailienant-core/tests/test_mcp_dispatch_guard.py
#
# Permission-matrix gate on McpToolAdapter._arun. A mutating remote tool must
# route through evaluate_action before any wire call: denied under plan mode,
# routed to human approval under the default mode (always for the most
# restricted tier), allowed under auto. Read-only tools are friction-free.
# The approval channel is injected so these tests never import the API layer.

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import mcp_adapter
from tools.mcp_adapter import McpToolAdapter


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
# Tests
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
async def test_hitl_without_channel_blocks_and_never_calls() -> None:
    """An HITL verdict with no session or no injected approval channel must
    refuse rather than silently calling an unapproved tool."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    adapter = _adapter("update_record", server_name="srv")

    no_session = await adapter._arun(
        arguments={}, session_id=None,
        session_permission_mode="DEFAULT", request_approval=_approval(True),
    )
    no_channel = await adapter._arun(
        arguments={}, session_id="s1",
        session_permission_mode="DEFAULT", request_approval=None,
    )

    assert "BLOCKED" in no_session and "BLOCKED" in no_channel
    session.call_tool.assert_not_awaited()


@pytest.mark.anyio
async def test_unwired_caller_falls_through_without_gate() -> None:
    """No session_permission_mode injected → the gate is skipped entirely
    (the contract engages only for a graph-wired dispatch that knows the mode)."""
    mcp_adapter._reset_mcp_session_for_tests()
    session = _seed_session("srv")

    adapter = _adapter("delete_everything", server_name="srv")
    result = await adapter._arun(arguments={"x": 1})

    assert result == {"ok": True}
    session.call_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# anyio backend constraint
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
