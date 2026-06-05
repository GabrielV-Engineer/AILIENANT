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
import ntpath
import uuid
from contextlib import AsyncExitStack
from typing import Any, Dict, FrozenSet, Sized, cast

from fastapi import APIRouter
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import core.db as catalog_db
from core.tool_rag import MCP_HANDSHAKE_TIMEOUT_SEC
from tools.mcp_adapter import _parse_mcp_uri

logger = logging.getLogger("MCP_API")

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

# S2 — command-injection defense for stdio:// MCP servers. A URI maps to an
# arbitrary executable, so we enforce a strict basename allowlist on BOTH the
# save and test paths. There is deliberately NO "any file that exists on disk"
# fallback (that would let stdio://../../bin/bash through). Full paths are
# accepted only when their basename is allowlisted; path-traversal is rejected.
_ALLOWED_MCP_COMMANDS: FrozenSet[str] = frozenset(
    {"npx", "npm", "node", "python", "python3", "py", "uv", "uvx", "deno", "docker"}
)
_POLICY_ERROR: str = "Command not allowed by system policy"


def _validate_mcp_command(uri: str) -> None:
    """Raise ValueError(_POLICY_ERROR) unless the stdio command is allowlisted.

    Logs the actual rejected command server-side only — the caller surfaces the
    generic ``_POLICY_ERROR`` to the client so attempted payloads never leak.
    """
    params = _parse_mcp_uri(uri)  # may raise ValueError for malformed URIs
    command = params.command
    # Reject path-traversal outright before any basename inspection. ntpath
    # splits on both "/" and "\" regardless of host OS.
    segments = command.replace("\\", "/").split("/")
    if ".." in segments:
        logger.warning("[mcp] rejected path-traversal command: %r", command)
        raise ValueError(_POLICY_ERROR)
    # Strip any directory + extension; compare the bare program name.
    basename = ntpath.basename(command)
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if stem.lower() not in _ALLOWED_MCP_COMMANDS:
        logger.warning("[mcp] rejected non-allowlisted command: %r", command)
        raise ValueError(_POLICY_ERROR)


@router.get("/servers")
async def list_servers() -> Dict[str, Any]:
    return {"servers": await catalog_db.list_mcp_servers()}


@router.post("/servers")
async def save_server(body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name", "")).strip()
    uri = str(body.get("uri", "")).strip()
    if not name or not uri:
        return {"ok": False, "error": "name and uri are required"}
    server_id = str(body.get("id") or uuid.uuid4().hex)
    transport = str(body.get("transport") or "stdio").strip()
    enabled = bool(body.get("enabled", True))
    if transport == "stdio":
        try:
            _validate_mcp_command(uri)  # S2 — reject non-allowlisted commands
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    await catalog_db.upsert_mcp_server(server_id, name, uri, transport, enabled)
    return {"ok": True, "servers": await catalog_db.list_mcp_servers()}


@router.delete("/servers/{server_id}")
async def remove_server(server_id: str) -> Dict[str, Any]:
    await catalog_db.delete_mcp_server(server_id)
    return {"ok": True, "servers": await catalog_db.list_mcp_servers()}


@router.post("/test")
async def test_server(body: Dict[str, Any]) -> Dict[str, Any]:
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
        _validate_mcp_command(uri)  # S2 — reject non-allowlisted commands pre-spawn
    except ValueError as exc:
        return {"reachable": False, "tool_count": 0, "error": str(exc)}

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
