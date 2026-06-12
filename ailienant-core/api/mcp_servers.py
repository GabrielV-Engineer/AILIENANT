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
from typing import TYPE_CHECKING, Any, Dict, Optional, Sized, cast

from fastapi import APIRouter, HTTPException
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import core.db as catalog_db
from core.config.mcp_secrets import (
    delete_server_secrets,
    mask_server_secrets,
    set_server_secrets,
)
from core.mcp_config import McpConfigError, export_mcp_config, import_mcp_config
from core.mcp_constants import ALLOWED_MCP_COMMANDS
from core.mcp_registry import get_regulated_server, serialize_registry
from core.tool_rag import MCP_HANDSHAKE_TIMEOUT_SEC
from tools import mcp_adapter
from tools.mcp_adapter import (
    _build_stdio_params,
    _parse_mcp_uri,
    bootstrap_mcp_session,
    build_stdio_uri,
    close_mcp_session,
)

if TYPE_CHECKING:
    from brain.state import AIlienantGraphState

logger = logging.getLogger("MCP_API")

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

# S2 — command-injection defense for stdio:// MCP servers. A URI maps to an
# arbitrary executable, so we enforce a strict basename allowlist on BOTH the
# save and test paths. The allowlist itself lives in core.mcp_constants so the
# curated registry shares the same policy. Path-traversal is rejected and full
# paths are accepted only when their basename is allowlisted.
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
    if stem.lower() not in ALLOWED_MCP_COMMANDS:
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
    # Resolve the row's name before deleting: secrets and live sessions are keyed
    # by server name, but the endpoint only carries the id. Wiping both keeps a
    # deleted credentialed server from leaving its token or stdio child behind.
    servers = await catalog_db.list_mcp_servers()
    name = next((s["name"] for s in servers if s["id"] == server_id), None)
    await catalog_db.delete_mcp_server(server_id)
    if name:
        delete_server_secrets(name)
        await close_mcp_session(name)
    return {"ok": True, "servers": await catalog_db.list_mcp_servers()}


@router.get("/registry")
async def list_registry() -> Dict[str, Any]:
    """Return the curated catalog of installable servers for the browse UI."""
    installed = {s["name"] for s in await catalog_db.list_mcp_servers()}
    return {"servers": serialize_registry(installed)}


@router.post("/registry/install")
async def install_from_registry(body: Dict[str, Any]) -> Dict[str, Any]:
    """Install a curated server: collect secrets, persist, and connect live.

    Only the vetted registry servers are installable here — install reuses the
    same command allowlist as the manual save path, so this opens no new attack
    surface. Secrets are stored backend-side and injected as env at connect time;
    they never enter the persisted uri.
    """
    name = str(body.get("name", "")).strip()
    server = get_regulated_server(name)
    if server is None:
        return {"ok": False, "error": "Unknown registry server."}

    raw_secrets = body.get("secrets") or {}
    if not isinstance(raw_secrets, dict):
        return {"ok": False, "error": "secrets must be an object of name -> value."}
    submitted = {str(k): str(v) for k, v in raw_secrets.items()}

    # Reject any secret key the server did not declare.
    unknown = set(submitted) - set(server.secrets)
    if unknown:
        return {"ok": False, "error": f"Unknown secret(s): {', '.join(sorted(unknown))}."}

    # Every declared secret must be provided now or already stored. A masked
    # placeholder counts as "already stored" (the value is preserved on merge).
    already = mask_server_secrets(name)
    for required in server.secrets:
        provided = submitted.get(required, "").strip()
        if not provided and not already.get(required):
            return {"ok": False, "error": f"Missing required secret: {required}."}

    if submitted:
        set_server_secrets(name, submitted)

    uri = build_stdio_uri(server.command, list(server.args))

    # Close any prior live session before re-bootstrapping — otherwise the
    # idempotent connect guard would keep the stale session and ignore the new
    # secret (and leak the old stdio child).
    if name in mcp_adapter._sessions:
        await close_mcp_session(name)

    await catalog_db.upsert_mcp_server(name, name, uri, "stdio", True)

    # Best-effort live connect so the server is usable without a host restart.
    # A failure (e.g. a cold npx download exceeding the handshake deadline) is
    # non-fatal: the row stays enabled and the next task or Test reconnects.
    state: Dict[str, Any] = {}
    reachable = await bootstrap_mcp_session(
        uri, cast("AIlienantGraphState", state), server_name=name
    )
    return {
        "ok": True,
        "reachable": reachable,
        "servers": await catalog_db.list_mcp_servers(),
    }


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
    server_name: Optional[str] = (str(body.get("server_name", "")).strip() or None)

    try:
        _validate_mcp_command(uri)  # S2 — reject non-allowlisted commands pre-spawn
    except ValueError as exc:
        return {"reachable": False, "tool_count": 0, "error": str(exc)}

    try:
        # Route through the launch builder so a PATH-only command resolves and a
        # credentialed server is probed with its stored secrets injected as env.
        params = _build_stdio_params(uri, server_name)
    except ValueError as exc:
        return {"reachable": False, "tool_count": 0, "error": f"invalid uri: {exc}"}
    if params is None:
        return {"reachable": False, "tool_count": 0, "error": "command not found on PATH"}

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


@router.get("/config/export")
async def export_config() -> Dict[str, Any]:
    """Return a portable, credential-free projection of the server catalog."""
    return await export_mcp_config()


@router.post("/config/import")
async def import_config(body: Dict[str, Any]) -> Dict[str, Any]:
    """Reconcile a config projection into the catalog (idempotent, name-keyed).

    A malformed or unsupported payload is rejected with HTTP 422. A valid
    payload whose individual servers fail the command allowlist is a partial
    success (HTTP 200) — the rejected servers are reported under ``skipped``.
    """
    try:
        return await import_mcp_config(body, validate_uri=_validate_mcp_command)
    except McpConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
