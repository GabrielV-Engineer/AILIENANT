# ailienant-core/tools/mcp_adapter.py
#
# Phase 2.18 — MCP Tool Adapter + Role-based Registry.
#
# McpToolAdapter wraps an MCP tool call behind the langchain_core BaseTool interface.
# McpToolRegistry provides a role-scoped store that Phase 2.19+ agents use via bind_tools().
#
# _call_mcp_tool() is intentionally a NotImplementedError stub — Phase 5 fills it
# with real MCP session/transport logic (mcp.ClientSession or equivalent).

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from enum import Enum
from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

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

    Lifecycle:
        Phase 2.18 — Registry infrastructure; _call_mcp_tool() is a stub.
        Phase 5     — _call_mcp_tool() is replaced with a real mcp.ClientSession call.

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
        Raises NotImplementedError (stub) until Phase 5 fills _call_mcp_tool().
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
        """Execute the MCP tool call over the wire.

        Phase 2.18: Raises NotImplementedError (stub).
        Phase 5 replacement:
            async with mcp.ClientSession(...) as session:
                return await session.call_tool(self.mcp_tool_name, arguments)
        """
        raise NotImplementedError(
            f"_call_mcp_tool() not implemented for tool '{self.mcp_tool_name}'. "
            "Phase 5 will wire this to a real MCP ClientSession."
        )


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
