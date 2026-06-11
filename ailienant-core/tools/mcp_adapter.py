# ailienant-core/tools/mcp_adapter.py
#
# MCP Tool Adapter + role-based registry. Provides a real stdio transport via
# mcp.ClientSession plus a bootstrap handshake that harvests tool schemas into
# the Tool RAG store and the SQLite tool_registry catalog. Sessions are tracked
# in a per-server registry whose lifetime is the host process; teardown is
# explicit via shutdown_mcp_sessions(), wired into the application lifespan.

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from collections import defaultdict
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    MutableMapping,
    Optional,
    Type,
)
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

# Human-approval window for a gated MCP tool call. Kept env-configurable so a
# slow remote round-trip does not require a code change to tune the deadline.
_MCP_HITL_TIMEOUT_SEC: float = float(os.environ.get("MCP_HITL_TIMEOUT_SEC", "120"))


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
    session opened for ``server_name`` by bootstrap_mcp_session().

    Permission gate:
        When a caller injects ``session_permission_mode`` (the runtime session
        policy), _arun() classifies the tool's privilege tier and consults the
        permission matrix before any remote call. A mutating tier is denied
        outright under plan mode, routed to human approval under the default
        mode (always for the most-restricted tier), and allowed under the
        auto mode. A read-only tier is friction-free. The approval channel is
        injected as ``request_approval`` so this module never imports the API
        layer.

    Timeout:
        asyncio.wait_for wraps _call_mcp_tool() with a configurable deadline (default 30s)
        to prevent Event Loop blocking on slow or unresponsive MCP servers.
    """

    name: str
    description: str
    args_schema: Type[BaseModel] = _McpToolInput

    mcp_tool_name: str = Field(description="Canonical name of the MCP tool on the server.")
    server_name: Optional[str] = Field(
        default=None,
        description=(
            "Name of the MCP server that owns this tool. Selects the session in "
            "the registry and scopes the curated privilege-tier override; None "
            "resolves to the default session."
        ),
    )
    timeout_s: float = Field(default=30.0, description="asyncio.wait_for deadline in seconds.")

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "McpToolAdapter is async-only. Use arun() / _arun() instead."
        )

    async def _arun(
        self,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        session_permission_mode: Optional[str] = None,
        request_approval: Optional[
            Callable[..., Awaitable[Optional[Dict[str, Any]]]]
        ] = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke the MCP tool asynchronously, gated by the permission matrix.

        session_id / session_permission_mode / request_approval are
        caller-injected runtime context, NOT model-chosen arguments — they are
        accepted here but kept OUT of args_schema so they never enter the
        tool-selection payload. The gate engages only when a caller supplies the
        session policy; an unwired caller falls straight through to the call.

        Raises asyncio.TimeoutError if the MCP call exceeds timeout_s.
        Raises RuntimeError if invoked before bootstrap_mcp_session() opened a session.
        """
        call_args: Dict[str, Any] = arguments or {}

        if session_permission_mode is not None:
            from core.permissions import (  # core-only import — no API-layer cycle
                PermissionDecision,
                PermissionMode,
                classify_tool_privilege,
                evaluate_action,
                session_mode_from_channel,
            )

            tier = classify_tool_privilege(
                self.mcp_tool_name, self.description, self.server_name
            )
            verdict = evaluate_action(
                session_mode_from_channel(session_permission_mode),
                tier,
                # Coder identity floor. A read-only tier short-circuits to ALLOW
                # before the floor is consulted, so a search-style tool is never
                # forced through approval.
                PermissionMode.EDIT_EXECUTE_RBW,
            )

            if verdict is PermissionDecision.DENY:
                return (
                    f"[mcp:{self.mcp_tool_name}] DENIED — plan mode is read-only; "
                    "tool not called."
                )

            if verdict is PermissionDecision.HITL:
                # No session or no approval channel means no way to surface the
                # request — refuse rather than silently calling an unapproved tool.
                if not session_id or request_approval is None:
                    return (
                        f"[mcp:{self.mcp_tool_name}] BLOCKED — approval required but "
                        "no channel is available to request it; tool not called."
                    )
                approval = await request_approval(
                    session_id=session_id,
                    action_description=f"MCP_TOOL_CALL: {self.mcp_tool_name}",
                    proposed_content=json.dumps(call_args, default=str)[:2000],
                    request_kind="MCP_TOOL_CALL",
                    timeout_s=_MCP_HITL_TIMEOUT_SEC,
                )
                if not approval or not approval.get("approved"):
                    return (
                        f"[mcp:{self.mcp_tool_name}] BLOCKED — tool call was not "
                        "approved; tool not called."
                    )

        logger.debug(
            "McpToolAdapter._arun: tool=%s server=%s args=%s timeout=%.1fs",
            self.mcp_tool_name, self.server_name, list(call_args.keys()), self.timeout_s,
        )
        return await asyncio.wait_for(
            self._call_mcp_tool(call_args),
            timeout=self.timeout_s,
        )

    async def _call_mcp_tool(self, arguments: Dict[str, Any]) -> Any:
        """Execute the MCP tool call over the wire via the bootstrapped session.

        Resolves the session for this tool's server in the registry. Raises
        RuntimeError if invoked before bootstrap_mcp_session() opened that
        session — call sites must connect the server on app startup.
        """
        key = self.server_name or _DEFAULT_SESSION_KEY
        session = _sessions.get(key)
        if session is None:
            raise RuntimeError(
                f"MCP session not bootstrapped; cannot call {self.mcp_tool_name!r}. "
                "Call tools.mcp_adapter.bootstrap_mcp_session(uri, state) first."
            )
        return await session.call_tool(self.mcp_tool_name, arguments)


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
# 5. STDIO TRANSPORT + BOOTSTRAP HANDSHAKE
# =====================================================================

# Per-server session registry. Lifetime is the host process; teardown is
# explicit via shutdown_mcp_sessions(), wired into the application lifespan.
# Each server keeps its own AsyncExitStack so one server's connect failure or
# re-connect never entangles or leaks another server's stdio process.
_DEFAULT_SESSION_KEY = "__default__"
_sessions: Dict[str, ClientSession] = {}
_exit_stacks: Dict[str, AsyncExitStack] = {}


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
    server_name: Optional[str] = None,
    timeout_sec: float = MCP_HANDSHAKE_TIMEOUT_SEC,
) -> bool:
    """Open a stdio MCP session, harvest tool schemas, populate the Tool RAG store.

    The session is registered under ``server_name`` (None resolves to the
    default key) so multiple enabled servers coexist and each adapter routes to
    its own server. Idempotent: a server already present in the registry is a
    no-op, so re-invocation never spawns a duplicate stdio process.

    Returns True on success (or when already connected), False on any fallback
    path (None URI, timeout, connection error). Never raises — failures are
    logged to state['permission_audit_log'] as event='tool_rag_fallback'.
    """
    key = server_name or _DEFAULT_SESSION_KEY

    audit_log: List[Dict[str, Any]] = state.setdefault("permission_audit_log", [])

    # Idempotent connect — never reopen a server that already holds a session.
    if key in _sessions:
        logger.debug("bootstrap_mcp_session: %r already connected — no-op.", key)
        return True

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
            privilege_tier=classify_tool_privilege(name, description, server_name),
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

    _sessions[key] = session
    _exit_stacks[key] = stack
    state["mcp_server_endpoint"] = uri
    audit_log.append(
        _audit_entry("mcp_bootstrap_success", uri=uri, discovered=discovered)
    )
    logger.info(
        "bootstrap_mcp_session: %d tools discovered from %r (server=%r).",
        discovered, uri, key,
    )
    return True


async def autoconnect_enabled_mcp_servers(
    state: Optional[MutableMapping[str, Any]] = None,
) -> int:
    """Connect every enabled MCP server in the catalog. Returns the count connected.

    Idempotent via bootstrap_mcp_session's skip-if-connected guard, so it is safe
    to call at app startup and again as a lazy first-task fallback. Never raises —
    each per-server failure is already absorbed into the audit log.
    """
    if state is None:
        state = {}

    from core.db import list_mcp_servers  # local import to avoid top-level cycles

    connected = 0
    for row in await list_mcp_servers():
        if not row.get("enabled"):
            continue
        ok = await bootstrap_mcp_session(
            row["uri"], state, server_name=row["name"]  # type: ignore[arg-type]
        )
        if ok:
            connected += 1
    logger.info("autoconnect_enabled_mcp_servers: %d server(s) connected.", connected)
    return connected


async def shutdown_mcp_sessions() -> None:
    """Close every open MCP session. The single teardown choke point.

    Each server's AsyncExitStack is closed best-effort so one failure does not
    block the rest, then the registry is cleared. Wired into the application
    lifespan shutdown so stdio child processes never outlive the host.
    """
    for key, stack in list(_exit_stacks.items()):
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.debug("shutdown_mcp_sessions: cleanup noise for %r.", key, exc_info=True)
    _exit_stacks.clear()
    _sessions.clear()


def _reset_mcp_session_for_tests() -> None:
    """Test-only: clear the session registry. NOT for production use.

    Mirrors the historical sync reset (drop references without awaiting a close);
    real teardown is covered by shutdown_mcp_sessions(). Safe to call outside an
    event loop.
    """
    _exit_stacks.clear()
    _sessions.clear()
