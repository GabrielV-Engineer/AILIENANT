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

from core.permissions import PermissionDecision
from gateway import catalog, governance, ledger

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


def _denied(reason: str, name: str, **extra: Any) -> List[types.TextContent]:
    """Build a denied envelope as MCP content.

    Bare denials (rate, budget) carry only the three core keys; richer reports
    (permission, human-approval degrade) attach the tier and caller guidance via
    ``extra``. Delegates to ``_envelope`` so there is a single serialization path.
    """
    return _envelope({"status": "denied", "reason": reason, "capability": name, **extra})


async def list_tools() -> List[types.Tool]:
    """Return the declared capability catalog as MCP tool descriptors."""
    return catalog.to_mcp_tools()


async def dispatch_call(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Govern and route a tool call, returning a structured envelope in every case.

    The pipeline runs the cheap DoS guard first (rate, then budget), then the
    symmetric permission gate, then the handler registry. Unknown verbs, throttled
    callers, denied permissions, and unwired verbs all return a machine-readable JSON
    result rather than a protocol error.
    """
    if catalog.get_capability(name) is None:
        return _envelope(
            {"status": "error", "reason": "unknown_capability", "capability": name}
        )

    caller_id = governance.resolve_caller_id()

    # DoS guard — every call is metered, READ_ONLY included; a call denied downstream
    # still spends its rate token, so probing floods are throttled regardless.
    if not await ledger.check_and_consume_rate(caller_id):
        return _denied("rate_exceeded", name)
    if await ledger.budget_exceeded(caller_id):
        return _denied("budget_exceeded", name)

    cap = catalog.get_capability(name)
    assert cap is not None  # re-checked above; narrows the Optional for the type checker
    decision = governance.authorize_invocation(cap)
    if decision is PermissionDecision.DENY:
        return _denied("permission_denied", name, tier=cap.tier.value)
    if decision is PermissionDecision.HITL:
        # The verb needs interactive human approval, but an external caller has no
        # human in its loop — calling for one would block on a click that never comes.
        # Degrade to an immediate, structured deny-report instead of hanging. Clients
        # that do have a human (our own extension) use the interactive REST+WS path.
        return _denied(
            "requires_human_approval",
            name,
            tier=cap.tier.value,
            would_have_required="human_approval",
            message=(
                "This capability requires interactive human approval, which the stdio "
                "gateway cannot provide (no human is in an external caller's loop). The "
                "request was denied without blocking. For an interactive approval path, "
                "drive this action through the AILIENANT VS Code extension."
            ),
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _envelope({"status": "not_implemented", "capability": name})
    result = await handler(arguments)
    return _envelope({"status": "ok", "capability": name, "result": result})


def build_gateway_server() -> Server:
    """Construct the MCP server with the catalog, governance, and routing registered."""
    governance.register_gateway_privileges()
    server: Server = Server(SERVER_NAME, version=PROTOCOL_VERSION)
    server.list_tools()(list_tools)
    server.call_tool()(dispatch_call)
    return server
