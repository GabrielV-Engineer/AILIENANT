"""External Capability Gateway — a stdio MCP server exposing AILIENANT verbs.

The gateway lets external agents (Claude Code, Codex) drive AILIENANT over the
Model Context Protocol. It is a pure adapter over the existing task-submit, token,
and WebSocket substrate; it adds no orchestration and owns no graph state.

Run it as a module: ``python -m gateway``.
"""
from __future__ import annotations

from gateway.catalog import CATALOG, Capability, to_mcp_tools
from gateway.server import PROTOCOL_VERSION, build_gateway_server, register_handler

__all__ = [
    "CATALOG",
    "Capability",
    "to_mcp_tools",
    "PROTOCOL_VERSION",
    "build_gateway_server",
    "register_handler",
]
