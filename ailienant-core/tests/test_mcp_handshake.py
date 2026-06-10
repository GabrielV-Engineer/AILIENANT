# ailienant-core/tests/test_mcp_handshake.py
#
# Phase 5.2 smoke tests for the MCP stdio bootstrap + singleton call path.
# Mocks mcp.client.stdio.stdio_client + mcp.ClientSession via AsyncMock so the
# tests never spawn a real subprocess.

from __future__ import annotations

import asyncio
import hashlib
import struct
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import AIlienantGraphState
from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore


# ---------------------------------------------------------------------------
# Helpers reused from test_tool_rag_selection.py
# ---------------------------------------------------------------------------


def _fake_embed_factory(dim: int = 8) -> Callable[[str], Awaitable[List[float]]]:
    async def _embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(dim):
            chunk = digest[(i * 4) % len(digest):(i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return _embed


def _make_isolated_store(tmp_path: Path) -> ToolRAGStore:
    return ToolRAGStore(
        embed_fn=_fake_embed_factory(),
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


def _new_state() -> Dict[str, Any]:
    return {"permission_audit_log": [], "mcp_server_endpoint": None}


def _make_descriptor(name: str, description: str, schema: Dict[str, Any]) -> MagicMock:
    """Build a stand-in for an mcp.Tool descriptor."""
    desc = MagicMock()
    desc.name = name
    desc.description = description
    desc.inputSchema = schema
    return desc


def _make_session_mock(tool_descriptors: List[MagicMock]) -> MagicMock:
    """Build a fake ClientSession whose async ctx mgr returns itself."""
    session = MagicMock()
    session.initialize = AsyncMock(return_value=None)
    list_result = MagicMock()
    list_result.tools = tool_descriptors
    session.list_tools = AsyncMock(return_value=list_result)
    session.call_tool = AsyncMock(return_value={"ok": True})
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_stdio_client_cm(streams: Any = ("read_stream", "write_stream")) -> MagicMock:
    """Build a fake stdio_client() async context manager that yields streams."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=streams)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bootstrap_with_no_uri_falls_back(tmp_path: Path) -> None:
    from tools import mcp_adapter

    mcp_adapter._reset_mcp_session_for_tests()
    state = _new_state()
    ok = await mcp_adapter.bootstrap_mcp_session(uri=None, state=cast(AIlienantGraphState, state))
    assert ok is False
    assert state["mcp_server_endpoint"] is None
    audit = state["permission_audit_log"]
    assert len(audit) == 1
    assert audit[0]["event"] == "tool_rag_fallback"
    assert audit[0]["reason"] == "no_uri"


@pytest.mark.anyio
async def test_bootstrap_with_invalid_uri_falls_back(tmp_path: Path) -> None:
    from tools import mcp_adapter

    mcp_adapter._reset_mcp_session_for_tests()
    state = _new_state()
    ok = await mcp_adapter.bootstrap_mcp_session(uri="http://nope/mcp", state=cast(AIlienantGraphState, state))
    assert ok is False
    assert state["mcp_server_endpoint"] is None
    assert state["permission_audit_log"][0]["reason"] == "invalid_uri"


@pytest.mark.anyio
async def test_bootstrap_with_valid_uri_populates_store(tmp_path: Path) -> None:
    from tools import mcp_adapter

    isolated_store = _make_isolated_store(tmp_path)
    mcp_adapter._reset_mcp_session_for_tests()

    descriptors = [
        _make_descriptor("RemoteSearch", "Search the web", {"type": "object"}),
        _make_descriptor("RemoteFetch", "Fetch a URL", {"type": "object"}),
        _make_descriptor("RemotePing", "Ping a host", {"type": "object"}),
    ]
    session = _make_session_mock(descriptors)
    stdio_cm = _make_stdio_client_cm()

    register_tool_mock = AsyncMock(return_value=None)

    state = _new_state()

    with (
        patch.object(mcp_adapter, "stdio_client", return_value=stdio_cm) as p_stdio,
        patch.object(mcp_adapter, "ClientSession", return_value=session) as p_session,
        patch.object(mcp_adapter, "tool_rag_store", isolated_store),
        patch("core.db.register_tool", register_tool_mock),
    ):
        ok = await mcp_adapter.bootstrap_mcp_session(
            uri="stdio:///fake/mcp-server",
            state=cast(AIlienantGraphState, state),
            timeout_sec=2.0,
        )

    assert ok is True
    assert state["mcp_server_endpoint"] == "stdio:///fake/mcp-server"
    # Three tools landed in the isolated Tool RAG store.
    schemas = isolated_store.all_schemas()
    names = {s.name for s in schemas}
    assert names == {"RemoteSearch", "RemoteFetch", "RemotePing"}
    # Tiers are classified fail-closed from the descriptor verb. "Search" and
    # "Fetch" carry a read verb and land READ_ONLY; "Ping" has no recognized
    # verb in its name or description, so it correctly falls to DANGEROUS
    # rather than being trusted as read-only.
    tiers = {s.name: s.privilege_tier for s in schemas}
    assert tiers["RemoteSearch"] is ToolPrivilegeTier.READ_ONLY
    assert tiers["RemoteFetch"] is ToolPrivilegeTier.READ_ONLY
    assert tiers["RemotePing"] is ToolPrivilegeTier.DANGEROUS
    # SQLite catalog received the same writes.
    assert register_tool_mock.await_count == 3
    # Audit log has the success event.
    success_entries = [
        e for e in state["permission_audit_log"] if e["event"] == "mcp_bootstrap_success"
    ]
    assert len(success_entries) == 1
    assert success_entries[0]["discovered"] == 3
    # Streams + session ctx managers were entered.
    p_stdio.assert_called_once()
    p_session.assert_called_once()
    session.initialize.assert_awaited_once()


@pytest.mark.anyio
async def test_bootstrap_timeout_falls_back(tmp_path: Path) -> None:
    from tools import mcp_adapter

    mcp_adapter._reset_mcp_session_for_tests()

    slow_cm = MagicMock()

    async def _slow_enter(*_args: Any, **_kwargs: Any) -> Any:
        # AsyncExitStack invokes type(cm).__aenter__(cm), which passes the cm
        # instance as a positional arg through the AsyncMock dispatcher; absorb
        # whatever it sends.
        await asyncio.sleep(10)
        return ("r", "w")

    slow_cm.__aenter__ = AsyncMock(side_effect=_slow_enter)
    slow_cm.__aexit__ = AsyncMock(return_value=False)

    state = _new_state()
    with patch.object(mcp_adapter, "stdio_client", return_value=slow_cm):
        ok = await mcp_adapter.bootstrap_mcp_session(
            uri="stdio:///never/responds",
            state=cast(AIlienantGraphState, state),
            timeout_sec=0.05,
        )

    assert ok is False
    assert state["mcp_server_endpoint"] is None
    fallback = [
        e for e in state["permission_audit_log"] if e["event"] == "tool_rag_fallback"
    ]
    assert any(e["reason"] == "handshake_timeout" for e in fallback)


@pytest.mark.anyio
async def test_call_mcp_tool_without_bootstrap_raises_runtime_error(tmp_path: Path) -> None:
    from tools.mcp_adapter import McpToolAdapter, _reset_mcp_session_for_tests

    _reset_mcp_session_for_tests()
    adapter = McpToolAdapter(
        name="anything",
        description="x",
        mcp_tool_name="anything",
    )
    with pytest.raises(RuntimeError, match="MCP session not bootstrapped"):
        await adapter._arun(arguments={})


@pytest.mark.anyio
async def test_call_mcp_tool_uses_singleton_after_bootstrap(tmp_path: Path) -> None:
    from tools import mcp_adapter

    isolated_store = _make_isolated_store(tmp_path)
    mcp_adapter._reset_mcp_session_for_tests()

    session = _make_session_mock([_make_descriptor("Echo", "Echo input", {})])
    stdio_cm = _make_stdio_client_cm()
    register_tool_mock = AsyncMock(return_value=None)

    state = _new_state()
    with (
        patch.object(mcp_adapter, "stdio_client", return_value=stdio_cm),
        patch.object(mcp_adapter, "ClientSession", return_value=session),
        patch.object(mcp_adapter, "tool_rag_store", isolated_store),
        patch("core.db.register_tool", register_tool_mock),
    ):
        ok = await mcp_adapter.bootstrap_mcp_session(
            uri="stdio:///fake/echo-server",
            state=cast(AIlienantGraphState, state),
            timeout_sec=2.0,
        )
        assert ok is True
        adapter = mcp_adapter.McpToolAdapter(
            name="Echo",
            description="Echo input",
            mcp_tool_name="Echo",
        )
        result = await adapter._arun(arguments={"hello": "world"})

    assert result == {"ok": True}
    session.call_tool.assert_awaited_once_with("Echo", {"hello": "world"})


# ---------------------------------------------------------------------------
# anyio backend constraint
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
