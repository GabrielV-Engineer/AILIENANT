"""The stdio MCP server for the External Capability Gateway.

This module wires the capability catalog into an MCP ``Server``: it answers
``list_tools`` with the declared catalog and routes ``call_tool`` through a handler
registry. The handler registry is the seam that later work populates with real
capability logic; until a verb is wired, the router returns a structured
``not_implemented`` envelope so an external caller gets a clean, machine-readable
response rather than a transport error.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List

import mcp.types as types
from mcp.server.lowlevel.server import Server

from gateway import catalog

# Semantic version of the gateway surface as a public contract. Breaking changes to
# the capability schemas bump this and trigger the deprecation policy.
PROTOCOL_VERSION = "0.1.0"

SERVER_NAME = "ailienant-gateway"

# A capability handler takes the call arguments and returns a JSON-serializable
# result. The registry is intentionally empty here; capability logic is wired
# incrementally as each verb lands.
Handler = Callable[[Dict[str, Any]], Awaitable[Any]]
_HANDLERS: Dict[str, Handler] = {}


def register_handler(name: str, handler: Handler) -> None:
    """Bind a capability name to its async handler."""
    _HANDLERS[name] = handler


def _envelope(payload: Dict[str, Any]) -> List[types.TextContent]:
    """Wrap a JSON payload as the single text-content result MCP expects."""
    return [types.TextContent(type="text", text=json.dumps(payload))]


async def list_tools() -> List[types.Tool]:
    """Return the declared capability catalog as MCP tool descriptors."""
    return catalog.to_mcp_tools()


async def dispatch_call(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Route a tool call to its handler, or return a structured fallback envelope.

    Unknown verbs return an ``error`` envelope; declared-but-unwired verbs return a
    ``not_implemented`` envelope. Both keep the transport healthy — a caller always
    gets a machine-readable JSON result rather than a protocol error.
    """
    if catalog.get_capability(name) is None:
        return _envelope(
            {"status": "error", "reason": "unknown_capability", "capability": name}
        )
    handler = _HANDLERS.get(name)
    if handler is None:
        return _envelope({"status": "not_implemented", "capability": name})
    result = await handler(arguments)
    return _envelope({"status": "ok", "capability": name, "result": result})


def build_gateway_server() -> Server:
    """Construct the MCP server with the catalog and routing seam registered."""
    server: Server = Server(SERVER_NAME, version=PROTOCOL_VERSION)
    server.list_tools()(list_tools)
    server.call_tool()(dispatch_call)
    return server
