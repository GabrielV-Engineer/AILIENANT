# ailienant-core/api/mcp_servers.py
# NOTE: deliberately NOT named api/mcp.py — that would shadow the third-party
# `mcp` package on sys.path and break `from mcp import ClientSession`.
"""Phase 7.9.A.7.e — MCP Server registry + zombie-safe connection probe.

CRUD persists the server list in the WAL-mode catalog DB. POST /test opens a
throwaway stdio MCP session purely to count the server's tools, then ruthlessly
reaps the spawned subprocess tree (it never touches the live process-singleton
session used by tools.mcp_adapter.bootstrap_mcp_session).

Auto-connecting saved servers at task time is a tracked follow-up.
"""
import asyncio
import logging
import uuid
from contextlib import AsyncExitStack
from typing import Sized, cast

from fastapi import APIRouter
from mcp import ClientSession  # type: ignore[attr-defined]  # mcp ships partial stubs
from mcp.client.stdio import stdio_client

import core.db as catalog_db
from core.tool_rag import MCP_HANDSHAKE_TIMEOUT_SEC
from tools.mcp_adapter import _parse_mcp_uri

logger = logging.getLogger("MCP_API")

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])


@router.get("/servers")
async def list_servers() -> dict:
    return {"servers": await catalog_db.list_mcp_servers()}


@router.post("/servers")
async def save_server(body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    uri = str(body.get("uri", "")).strip()
    if not name or not uri:
        return {"ok": False, "error": "name and uri are required"}
    server_id = str(body.get("id") or uuid.uuid4().hex)
    transport = str(body.get("transport") or "stdio").strip()
    enabled = bool(body.get("enabled", True))
    await catalog_db.upsert_mcp_server(server_id, name, uri, transport, enabled)
    return {"ok": True, "servers": await catalog_db.list_mcp_servers()}


@router.delete("/servers/{server_id}")
async def remove_server(server_id: str) -> dict:
    await catalog_db.delete_mcp_server(server_id)
    return {"ok": True, "servers": await catalog_db.list_mcp_servers()}


@router.post("/test")
async def test_server(body: dict) -> dict:
    """Probe an MCP server: connect, list tools, report count. Always reaps.

    Zombie-safety: the whole probe runs inside `async with stack`, so the
    SDK's stdio_client cleanup (SIGTERM -> SIGKILL process-tree termination)
    runs in THIS coroutine's frame on every exit path. The remote calls are
    each bounded by MCP_HANDSHAKE_TIMEOUT_SEC, so a hung server cannot stall
    the probe past the deadline.
    """
    uri = str(body.get("uri", "")).strip()
    if not uri:
        return {"reachable": False, "tool_count": 0, "error": "uri is required"}

    try:
        params = _parse_mcp_uri(uri)
    except ValueError as exc:
        return {"reachable": False, "tool_count": 0, "error": f"invalid uri: {exc}"}

    try:
        async with AsyncExitStack() as stack:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=MCP_HANDSHAKE_TIMEOUT_SEC)
            result = await asyncio.wait_for(session.list_tools(), timeout=MCP_HANDSHAKE_TIMEOUT_SEC)
            tools = getattr(result, "tools", result) or []
            return {"reachable": True, "tool_count": len(cast(Sized, tools))}
    except asyncio.TimeoutError:
        logger.warning("[mcp_test] handshake timeout for %r (subprocess reaped).", uri)
        return {"reachable": False, "tool_count": 0, "error": "handshake timeout"}
    except (ConnectionError, OSError, ValueError) as exc:
        logger.warning("[mcp_test] connect error for %r: %s", uri, exc)
        return {"reachable": False, "tool_count": 0, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — probe must never 500; report instead
        logger.warning("[mcp_test] unexpected error for %r: %s", uri, exc)
        return {"reachable": False, "tool_count": 0, "error": str(exc)}
