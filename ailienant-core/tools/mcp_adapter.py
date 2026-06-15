# ailienant-core/tools/mcp_adapter.py
#
# MCP Tool Adapter + role-based registry. Provides a real stdio transport via
# mcp.ClientSession plus a bootstrap handshake that harvests tool schemas into
# the Tool RAG store and the SQLite tool_registry catalog. Sessions are tracked
# in a per-server registry whose lifetime is the host process; teardown is
# explicit via shutdown_mcp_sessions(), wired into the application lifespan.

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import shlex
import shutil
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
    Set,
    Type,
)
from urllib.parse import parse_qsl, urlencode, urlparse

from langchain_core.tools import BaseTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
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
# AMBIENT SESSION CONTEXT
# =====================================================================
# These ContextVars are set by task_service before the LangGraph run so
# McpToolAdapter._arun can consult the session policy even when LangChain
# invokes the tool without explicit session kwargs.  Each contextvar token
# is reset in the task_service finally block, so leakage across tasks is
# impossible regardless of how a task exits.
_task_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_mcp_task_session_id", default=None
)
_task_session_mode: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_mcp_task_session_mode", default=None
)

# =====================================================================
# SESSION-SCOPED TRUST VALVE
# =====================================================================
# After a human explicitly approves an MCP tool call, subsequent calls
# to the same tool within the same task session skip the HITL prompt.
# Keyed by (session_id, tool_name).  Cleared by clear_session_trust()
# when the task finishes (called from the task_service finally block).
_session_trust: Dict[str, Set[str]] = defaultdict(set)


def _is_session_trusted(session_id: str, tool_name: str) -> bool:
    return tool_name in _session_trust.get(session_id, set())


def _grant_session_trust(session_id: str, tool_name: str) -> None:
    _session_trust[session_id].add(tool_name)


def clear_session_trust(session_id: str) -> None:
    """Remove all per-session trust grants for a finished task.

    Called from the task_service finally block so trust never bleeds
    across task boundaries.  No-op when the session is unknown.
    """
    _session_trust.pop(session_id, None)


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
        tool-selection payload.

        When invoked via LangChain's normal tool-call path these kwargs are
        absent, so the gate resolves context from the ambient ContextVars set
        by task_service before the graph runs.  An unwired caller (no kwargs
        AND no ContextVar) falls straight through — zero behavioral change for
        tests and callers that predate the gate.

        Raises asyncio.TimeoutError if the MCP call exceeds timeout_s.
        Raises RuntimeError if invoked before bootstrap_mcp_session() opened a session.
        """
        call_args: Dict[str, Any] = arguments or {}

        # Resolve session context — explicit kwargs take priority; fall back to
        # the ambient values set by task_service before the LangGraph run.
        effective_session_id = session_id or _task_session_id.get()
        effective_mode = session_permission_mode or _task_session_mode.get()

        if effective_mode is not None:
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

            # Trust-once valve: skip HITL if the user already approved this
            # tool during the current task session.
            if effective_session_id and _is_session_trusted(
                effective_session_id, self.mcp_tool_name
            ):
                pass  # trusted — fall through to the wire call below
            else:
                verdict = evaluate_action(
                    session_mode_from_channel(effective_mode),
                    tier,
                    # Coder identity floor. A read-only tier short-circuits to
                    # ALLOW before the floor is consulted, so a search-style
                    # tool is never forced through approval.
                    PermissionMode.EDIT_EXECUTE_RBW,
                )

                if verdict is PermissionDecision.DENY:
                    return (
                        f"[mcp:{self.mcp_tool_name}] DENIED — plan mode is read-only; "
                        "tool not called."
                    )

                if verdict is PermissionDecision.HITL:
                    # No session means we cannot deliver the request over the WS.
                    if not effective_session_id:
                        return (
                            f"[mcp:{self.mcp_tool_name}] BLOCKED — approval required "
                            "but no session is available to route the request; tool not called."
                        )

                    # When no approval callable was injected (the common path for
                    # a LangChain-orchestrated call), build one from vfs_manager —
                    # the same lazy-import pattern used by sandbox.py and supervisor.py.
                    effective_approval: Callable[..., Awaitable[Optional[Dict[str, Any]]]]
                    if request_approval is not None:
                        effective_approval = request_approval
                    else:
                        from api.websocket_manager import vfs_manager  # lazy — no cycle
                        async def _default_approval(**kw: Any) -> Optional[Dict[str, Any]]:
                            return await vfs_manager.request_human_approval(**kw)
                        effective_approval = _default_approval

                    approval = await effective_approval(
                        session_id=effective_session_id,
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
                    # Record the approval so subsequent calls to this tool within
                    # the same task session skip the HITL prompt.
                    _grant_session_trust(effective_session_id, self.mcp_tool_name)

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
    """Parse a ``stdio://`` URI into StdioServerParameters.

    Two command forms are accepted:
      * absolute path — ``stdio:///abs/path/to/server`` (the path is the
        executable; on Windows ``stdio:///C:/path/to/exe`` is also accepted);
      * bare command — ``stdio://npx`` (an empty path falls back to the netloc;
        the program is resolved from PATH at launch).

    The query string contributes positional arguments (one ``arg=value`` pair per
    argument). Values are percent-decoded then shell-split, so an argument
    carrying ``&`` or ``=`` survives intact when it was URL-encoded on the way in.
    Any query key other than ``arg`` is rejected loudly.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "stdio":
        raise ValueError(
            f"_parse_mcp_uri: unsupported scheme {parsed.scheme!r}. "
            "Only stdio:// transports are supported."
        )

    # An absolute-path executable rides in the path; a bare PATH-resolved command
    # (npx, uvx) has an empty path and rides in the netloc instead.
    command = parsed.path or parsed.netloc
    if not command:
        raise ValueError(f"_parse_mcp_uri: missing executable in URI {uri!r}.")

    # The leading slash on absolute paths is preserved by urlparse; on Windows
    # callers can pass stdio:///C:/path/to/exe and we strip the leading slash
    # only when followed by a drive letter.
    if len(command) >= 3 and command[0] == "/" and command[2] == ":":
        command = command[1:]

    args: List[str] = []
    if parsed.query:
        # parse_qsl percent-decodes and splits on '&'/'=' correctly; shlex then
        # honors quoting within a single decoded value.
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key != "arg":
                raise ValueError(
                    f"_parse_mcp_uri: only 'arg' query keys supported, got {key!r}."
                )
            args.extend(shlex.split(value))

    return StdioServerParameters(command=command, args=args)


def build_stdio_uri(command: str, args: List[str]) -> str:
    """Compose a ``stdio://`` URI from a command and its arguments.

    The inverse of :func:`_parse_mcp_uri` for bare commands. Each argument is
    URL-encoded so special characters (``&``, ``=``, spaces) round-trip safely —
    never naive string concatenation.
    """
    query = urlencode([("arg", a) for a in args])
    return f"stdio://{command}?{query}" if query else f"stdio://{command}"


def _resolve_server_env(server_name: Optional[str]) -> Dict[str, str]:
    """Return the stored secret env vars a curated server declares it needs.

    Looks up the server in the curated registry to learn which environment
    variable NAMES it expects, then pulls the matching stored VALUES from the
    secret store. A manual server, an unknown name, or a server with no stored
    secrets yields ``{}`` — the regression-safe default that leaves launch
    behavior unchanged.
    """
    if not server_name:
        return {}
    # Local imports keep this module's import graph cycle-free.
    from core.config.mcp_secrets import get_server_env
    from core.mcp_registry import get_regulated_server

    server = get_regulated_server(server_name)
    if server is None or not server.secrets:
        return {}
    stored = get_server_env(server_name)
    return {name: stored[name] for name in server.secrets if name in stored}


def _build_stdio_params(
    uri: str, server_name: Optional[str] = None
) -> Optional[StdioServerParameters]:
    """Build launch parameters, hardening command resolution and env injection.

    Returns ``None`` (never raises here) when a bare command cannot be resolved
    on PATH, so the caller can degrade gracefully instead of crashing.

      * Command resolution: a bare command (no path separator) is resolved via
        ``shutil.which`` so a PATH-only launcher like ``npx`` (``npx.cmd`` on
        Windows) is handed to the SDK as an absolute path — ``shell=True`` is
        never used. A command that already contains a separator is passed
        through unchanged.
      * Secret injection: when the server has stored secrets, they are merged
        on top of the SDK's default (platform-critical) environment so the child
        inherits PATH/HOME/APPDATA etc. without leaking the full host
        environment. With no secrets the env is left as ``None`` (the SDK applies
        the same default internally).
    """
    params = _parse_mcp_uri(uri)
    command = params.command

    if not any(sep in command for sep in ("/", "\\", os.sep)):
        resolved = shutil.which(command)
        if resolved is None:
            logger.warning(
                "_build_stdio_params: command %r not found on PATH.", command
            )
            return None
        command = resolved

    secrets = _resolve_server_env(server_name)
    env: Optional[Dict[str, str]] = None
    if secrets:
        env = {**get_default_environment(), **secrets}

    return StdioServerParameters(command=command, args=params.args, env=env)


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
        params = _build_stdio_params(uri, server_name)
    except ValueError as exc:
        state["mcp_server_endpoint"] = None
        audit_log.append(
            _audit_entry("tool_rag_fallback", reason="invalid_uri", detail=str(exc))
        )
        logger.warning("bootstrap_mcp_session: invalid URI %r: %s", uri, exc)
        return False
    if params is None:
        state["mcp_server_endpoint"] = None
        audit_log.append(
            _audit_entry("tool_rag_fallback", reason="command_not_found", detail=uri)
        )
        logger.warning("bootstrap_mcp_session: launcher command not found for %r.", uri)
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


async def close_mcp_session(server_name: str) -> None:
    """Close a single server's session and drop it from the registry.

    The per-key counterpart of :func:`shutdown_mcp_sessions`. A re-install must
    call this before reconnecting: ``bootstrap_mcp_session`` short-circuits when a
    key is already present, so without an explicit close the stale session would
    survive (the new credentials would never take effect and the old stdio child
    would leak). No-op when the server is not connected.
    """
    stack = _exit_stacks.pop(server_name, None)
    _sessions.pop(server_name, None)
    if stack is not None:
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.debug("close_mcp_session: cleanup noise for %r.", server_name, exc_info=True)


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


# =====================================================================
# Brave-search adapter — search callable for the analyst tools
# =====================================================================

_BRAVE_SERVER_NAME: str = "brave-search"
_BRAVE_SEARCH_TOOL: str = "search"
_SEARCH_TIMEOUT_SEC: float = float(os.environ.get("BRAVE_SEARCH_TIMEOUT_SEC", "20"))
_SEARCH_RESULT_CAP: int = 8000
# Match the analyst tools' own degradation string so a missing session reads the
# same whether the search_fn is absent or merely unconnected.
_SEARCH_UNAVAILABLE: str = "search provider unavailable"


def _extract_mcp_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into plain text, tolerating odd shapes.

    A well-formed result exposes ``.content`` as a list of content blocks whose
    text blocks carry ``.text``. We never trust that shape blindly: a non-list, a
    missing attribute, or a non-text block each degrade to ``str(...)`` rather than
    raising.
    """
    content = getattr(result, "content", result)
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            text = getattr(item, "text", None)
            parts.append(str(text) if text is not None else str(item))
        return "\n".join(parts)
    return str(content)


def make_brave_search_fn(
    server_name: str = _BRAVE_SERVER_NAME,
) -> Callable[[str, int], Awaitable[str]]:
    """Build a search callable backed by the brave-search MCP session.

    The returned coroutine matches the analyst tools' injection signature
    ``(query, max_results) -> str``. It resolves the live session lazily at call
    time (so it tolerates connect/disconnect), bounds the result, and **degrades to
    the standard "unavailable" string on a missing session OR any wire fault** — the
    `await call_tool` is wrapped in a timeout + broad except so it can never raise
    into the calling graph node. The transport child process is owned by the MCP
    session (reaped at session teardown), so cancelling the RPC orphans nothing.
    """

    async def _search(query: str, max_results: int) -> str:
        session = _sessions.get(server_name)
        if session is None:
            return _SEARCH_UNAVAILABLE
        try:
            result = await asyncio.wait_for(
                session.call_tool(
                    _BRAVE_SEARCH_TOOL,
                    {"query": query, "count": max(1, min(int(max_results), 10))},
                ),
                timeout=_SEARCH_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 — resilience: never crash the node
            logger.warning(
                "make_brave_search_fn: search failed for %r: %s", query, exc, exc_info=True
            )
            return _SEARCH_UNAVAILABLE
        return _extract_mcp_text(result)[:_SEARCH_RESULT_CAP]

    return _search
