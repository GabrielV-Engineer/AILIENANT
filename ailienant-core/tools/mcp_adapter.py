# ailienant-core/tools/mcp_adapter.py
#
# Phase 2.18 — MCP Tool Adapter + Role-based Registry.
# Phase 5.2 — Real stdio transport via mcp.ClientSession + bootstrap handshake
#             that harvests tool schemas into the Tool RAG store and the SQLite
#             tool_registry catalog. Singleton session lifetime is process-scoped;
#             cleanup relies on OS-delivered EOF when stdin/stdout close.

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections import defaultdict
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

from core.permissions import classify_tool_privilege
from core.tool_rag import MCP_HANDSHAKE_TIMEOUT_SEC, ToolSchema, tool_rag_store

if TYPE_CHECKING:
    from brain.state import AIlienantGraphState

logger = logging.getLogger("MCP_ADAPTER")


# =====================================================================
# 1. AGENT ROLES
# =====================================================================

class AgentRole(str, Enum):
    """High-level agent roles for role-scoped tool binding.

    Maps to the four cognitive nodes in the LangGraph topology, NOT to
    WBSStep.target_role (CoderAgent sub-personalities). Phase 2.19+ agents call:
        llm = tool_registry.bind_tools(base_llm, AgentRole.CODER)
    """
    PLANNER = "planner"
    CODER = "coder"
    ANALYST = "analyst"
    ORCHESTRATOR = "orchestrator"
    RESEARCHER = "researcher"


# =====================================================================
# 2. MCP TOOL ADAPTER
# =====================================================================

class _McpToolInput(BaseModel):
    """Free-form input schema for McpToolAdapter.

    A single arguments dict is used because MCP tool schemas are dynamic
    and not known until Phase 5 wires up real MCP sessions. Concrete
    subclasses in Phase 5 can narrow this to typed schemas.
    """
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments forwarded verbatim to the MCP tool call.",
    )


class McpToolAdapter(BaseTool):
    """LangChain BaseTool wrapper around a single MCP tool endpoint.

    _call_mcp_tool() dispatches a real mcp.ClientSession.call_tool() over the
    process-singleton session opened by bootstrap_mcp_session().

    Timeout:
        asyncio.wait_for wraps _call_mcp_tool() with a configurable deadline (default 30s)
        to prevent Event Loop blocking on slow or unresponsive MCP servers.
    """

    name: str
    description: str
    args_schema: Type[BaseModel] = _McpToolInput

    mcp_tool_name: str = Field(description="Canonical name of the MCP tool on the server.")
    timeout_s: float = Field(default=30.0, description="asyncio.wait_for deadline in seconds.")

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "McpToolAdapter is async-only. Use arun() / _arun() instead."
        )

    async def _arun(self, arguments: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        """Invoke the MCP tool asynchronously with a hard timeout.

        Raises asyncio.TimeoutError if the MCP call exceeds timeout_s.
        Raises RuntimeError if invoked before bootstrap_mcp_session() opened a session.
        """
        call_args: Dict[str, Any] = arguments or {}
        logger.debug(
            "McpToolAdapter._arun: tool=%s args=%s timeout=%.1fs",
            self.mcp_tool_name, list(call_args.keys()), self.timeout_s,
        )
        return await asyncio.wait_for(
            self._call_mcp_tool(call_args),
            timeout=self.timeout_s,
        )

    async def _call_mcp_tool(self, arguments: Dict[str, Any]) -> Any:
        """Execute the MCP tool call over the wire via the bootstrapped session.

        Phase 5.2: uses the process-singleton mcp.ClientSession opened by
        bootstrap_mcp_session(). Raises RuntimeError if invoked before bootstrap
        — call sites must invoke bootstrap_mcp_session(...) on app startup.
        """
        if _session_singleton is None:
            raise RuntimeError(
                f"MCP session not bootstrapped; cannot call {self.mcp_tool_name!r}. "
                "Call tools.mcp_adapter.bootstrap_mcp_session(uri, state) first."
            )
        return await _session_singleton.call_tool(self.mcp_tool_name, arguments)


# =====================================================================
# 3. MCP TOOL REGISTRY
# =====================================================================

class McpToolRegistry:
    """Role-scoped registry for McpToolAdapter instances.

    Usage (Phase 2.19+):
        tool_registry.register(AgentRole.CODER, some_mcp_adapter)
        llm_with_tools = tool_registry.bind_tools(base_llm, AgentRole.CODER)

    bind_tools() returns llm unchanged when no tools are registered for the role
    so callers never need to guard against empty registries.
    """

    def __init__(self) -> None:
        self._tools: Dict[AgentRole, List[McpToolAdapter]] = defaultdict(list)

    def register(self, role: AgentRole, tool: McpToolAdapter) -> None:
        """Add a tool to the registry under the given agent role."""
        self._tools[role].append(tool)
        logger.info(
            "McpToolRegistry.register: role=%s tool=%s (total for role: %d)",
            role.value, tool.name, len(self._tools[role]),
        )

    def get_tools(self, role: AgentRole) -> List[McpToolAdapter]:
        """Return all tools registered for role (empty list if none)."""
        return list(self._tools.get(role, []))

    def bind_tools(self, llm: Any, role: AgentRole) -> Any:
        """Bind all registered tools for role to llm via llm.bind_tools().

        Returns llm unchanged if no tools registered for role.

        Args:
            llm: A LangChain ChatModel or RunnableBinding exposing bind_tools().
            role: The AgentRole whose tools should be bound.
        """
        tools = self.get_tools(role)
        if not tools:
            logger.debug(
                "McpToolRegistry.bind_tools: no tools for role=%s — llm unchanged.",
                role.value,
            )
            return llm
        logger.info(
            "McpToolRegistry.bind_tools: binding %d tool(s) to llm for role=%s.",
            len(tools), role.value,
        )
        return llm.bind_tools(tools)


# =====================================================================
# 4. MODULE-LEVEL SINGLETON
# =====================================================================

# Imported by Phase 2.19+ agents:
#   from tools.mcp_adapter import tool_registry, AgentRole
#   llm = tool_registry.bind_tools(base_llm, AgentRole.CODER)
tool_registry = McpToolRegistry()


# =====================================================================
# 5. PHASE 5.2 — STDIO TRANSPORT + BOOTSTRAP HANDSHAKE
# =====================================================================

# Module-level singletons. Lifetime is the entire Python process; cleanup
# is delegated to OS-delivered EOF on stdin/stdout when the process exits
# (see PHASE_5_BLUEPRINT Flag C in the plan). No atexit hook for the
# async session — that pattern is unreliable once the event loop stops.
_session_singleton: Optional[ClientSession] = None
_exit_stack: Optional[AsyncExitStack] = None


def _parse_mcp_uri(uri: str) -> StdioServerParameters:
    """Parse 'stdio:///abs/path/to/server[?arg=x&arg=y]' into StdioServerParameters.

    The path component is the executable; the query string contributes positional
    arguments (one ?arg=value pair per arg). Anything else is rejected loudly.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "stdio":
        raise ValueError(
            f"_parse_mcp_uri: unsupported scheme {parsed.scheme!r}. "
            "Phase 5.2 only supports stdio:// transports."
        )
    if not parsed.path:
        raise ValueError(f"_parse_mcp_uri: missing executable path in URI {uri!r}.")

    # The leading slash on absolute paths is preserved by urlparse; on Windows
    # callers can pass stdio:///C:/path/to/exe and we strip the leading slash
    # only when followed by a drive letter.
    command = parsed.path
    if len(command) >= 3 and command[0] == "/" and command[2] == ":":
        command = command[1:]

    args: List[str] = []
    if parsed.query:
        # Format: arg=foo&arg=bar — preserve order; values may be shell-quoted.
        for pair in parsed.query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            if key != "arg":
                raise ValueError(
                    f"_parse_mcp_uri: only 'arg' query keys supported, got {key!r}."
                )
            args.extend(shlex.split(value))

    return StdioServerParameters(command=command, args=args)


def _audit_entry(event: str, **extra: Any) -> Dict[str, Any]:
    """Build one permission_audit_log entry with consistent shape."""
    entry: Dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    entry.update(extra)
    return entry


async def bootstrap_mcp_session(
    uri: Optional[str],
    state: "AIlienantGraphState",
    *,
    timeout_sec: float = MCP_HANDSHAKE_TIMEOUT_SEC,
) -> bool:
    """Open a stdio MCP session, harvest tool schemas, populate the Tool RAG store.

    Returns True on success, False on any fallback path (None URI, timeout,
    connection error). Never raises — failures are logged to
    state['permission_audit_log'] as event='tool_rag_fallback'.
    """
    global _session_singleton, _exit_stack

    audit_log: List[Dict[str, Any]] = state.setdefault("permission_audit_log", [])

    if uri is None:
        state["mcp_server_endpoint"] = None
        audit_log.append(_audit_entry("tool_rag_fallback", reason="no_uri"))
        logger.info("bootstrap_mcp_session: no URI provided — local-only fallback.")
        return False

    try:
        params = _parse_mcp_uri(uri)
    except ValueError as exc:
        state["mcp_server_endpoint"] = None
        audit_log.append(
            _audit_entry("tool_rag_fallback", reason="invalid_uri", detail=str(exc))
        )
        logger.warning("bootstrap_mcp_session: invalid URI %r: %s", uri, exc)
        return False

    stack = AsyncExitStack()
    try:
        async def _open_and_initialise() -> ClientSession:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            return session

        session = await asyncio.wait_for(_open_and_initialise(), timeout=timeout_sec)
    except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
        # Best-effort cleanup of whatever we did manage to open.
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 — diagnostic-only path
            logger.debug("bootstrap_mcp_session: cleanup-after-failure noise.", exc_info=True)
        state["mcp_server_endpoint"] = None
        reason = "handshake_timeout" if isinstance(exc, asyncio.TimeoutError) else "connect_error"
        audit_log.append(
            _audit_entry("tool_rag_fallback", reason=reason, detail=str(exc))
        )
        logger.warning("bootstrap_mcp_session: %s for %r (%s)", reason, uri, exc)
        return False

    # Harvest tool schemas. Any per-tool failure is logged but does not abort
    # the whole bootstrap — partial discovery is still useful.
    try:
        list_result = await session.list_tools()
        descriptors = getattr(list_result, "tools", list_result) or []
    except Exception as exc:  # noqa: BLE001 — list_tools is the one remote call we tolerate failing
        await stack.aclose()
        state["mcp_server_endpoint"] = None
        audit_log.append(
            _audit_entry("tool_rag_fallback", reason="list_tools_failed", detail=str(exc))
        )
        logger.warning("bootstrap_mcp_session: list_tools failed for %r: %s", uri, exc)
        return False

    from core.db import register_tool  # local import to avoid top-level cycles

    discovered = 0
    for descriptor in descriptors:
        name = getattr(descriptor, "name", None)
        if not name:
            continue
        description = getattr(descriptor, "description", "") or ""
        input_schema = getattr(descriptor, "inputSchema", None) or {}
        try:
            json_schema = json.dumps(input_schema, default=str)
        except (TypeError, ValueError):
            json_schema = "{}"

        # The tool name and description come from an untrusted external
        # server, so the tier is classified fail-closed: an unrecognized
        # verb resolves to the most-restricted tier rather than READ_ONLY,
        # ensuring a mutating remote tool cannot slip past the approval gate.
        schema_obj = ToolSchema(
            name=name,
            description=description,
            json_schema=json_schema,
            privilege_tier=classify_tool_privilege(name, description),
            allowed_roles=frozenset({"core_dev"}),  # safe default
        )
        try:
            await tool_rag_store.register_schema(schema_obj)
            await register_tool(name, description, json_schema, mcp_privilege=False)
            discovered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bootstrap_mcp_session: failed to register %r: %s", name, exc
            )

    _session_singleton = session
    _exit_stack = stack
    state["mcp_server_endpoint"] = uri
    audit_log.append(
        _audit_entry("mcp_bootstrap_success", uri=uri, discovered=discovered)
    )
    logger.info("bootstrap_mcp_session: %d tools discovered from %r.", discovered, uri)
    return True


def _reset_mcp_session_for_tests() -> None:
    """Test-only: clear the singletons. NOT for production use."""
    global _session_singleton, _exit_stack
    _session_singleton = None
    _exit_stack = None
