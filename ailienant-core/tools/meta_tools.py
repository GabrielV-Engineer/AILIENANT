"""Meta-tools — tools that operate over the tool catalog itself.

``ToolSearchTool`` (``tool_search``) is the universal discovery primitive that
makes a large catalog fit a small prompt: when most tools are deferred out of
the context, an agent calls it with a natural-language capability query and gets
back the relevant tool names + descriptions.

It is a DISCOVERY tool, not a direct-load. The returned listing is NOT directly
callable — LangChain/LangGraph needs a tool's full JSON schema in the model's
``tools`` array to structure a call. So the result is the listing plus a
deterministic shift-left instruction telling the agent to NAME the tool in its
next plan/output; the intent-based selection node then injects that tool's full
schema on the following execution cycle. The full ``json_schema`` never
round-trips through the result, preserving the very token budget retrieval saved.

Role resolution is config-first: the live per-step role is read from the injected
``RunnableConfig`` when the call site threads it; otherwise it falls back to the
ambient ``_task_active_role`` ContextVar. That fallback is captured once per task
and is therefore stale across per-WBS-step role transitions — a declared MVP
trade-off (see DEBT-039). Because ``tool_search`` is READ_ONLY, a stale role can
never escalate privilege: worst case it under- or over-scopes a read-only
discovery listing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Type

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import (
    TOOL_RAG_TOP_K,
    ToolRAGStore,
    ToolSchema,
    tool_rag_store,
)
from tools.control_tools import _CONTROL_ROLES

logger = logging.getLogger("META_TOOLS")

_DEFAULT_ROLE: str = "core_dev"


# =====================================================================
# Role / session-mode resolution (config-first, ContextVar fallback)
# =====================================================================


def _resolve_active_role(config: Optional[RunnableConfig]) -> str:
    """Live per-step role from RunnableConfig; ambient ContextVar as MVP fallback."""
    if config:
        configurable = config.get("configurable") or {}
        role = configurable.get("active_role")
        if role:
            return str(role)
    # Fallback: ambient context captured at task entry (DEBT-039 — stale across
    # per-step role transitions; robust config threading lands in 8.8.5).
    from tools.mcp_adapter import _task_active_role

    return _task_active_role.get() or _DEFAULT_ROLE


def _resolve_session_mode(config: Optional[RunnableConfig]) -> SessionPermissionMode:
    """Live session mode from RunnableConfig; ambient ContextVar as fallback."""
    raw: Optional[str] = None
    if config:
        configurable = config.get("configurable") or {}
        raw = configurable.get("session_permission_mode")
    if raw is None:
        from tools.mcp_adapter import _task_session_mode

        raw = _task_session_mode.get()
    if raw:
        try:
            return SessionPermissionMode(str(raw).lower())
        except ValueError:
            logger.debug("tool_search: unknown session_mode %r — using DEFAULT", raw)
    return SessionPermissionMode.DEFAULT


# =====================================================================
# ToolSearchTool
# =====================================================================


class ToolSearchInput(BaseModel):
    query: str = Field(
        description="Natural-language description of the capability you need."
    )
    k: int = Field(
        default=TOOL_RAG_TOP_K,
        ge=1,
        le=TOOL_RAG_TOP_K,
        description="Max number of tool schemas to return (capped at 5).",
    )


class ToolSearchTool(BaseTool):
    """Retrieve the most relevant tool schemas for a described capability.

    READ_ONLY and available to every role, so it is admitted under any session
    mode (including PLAN). Returns names + descriptions plus a shift-left
    instruction — never full schemas (token hygiene).
    """

    name: str = "tool_search"
    description: str = (
        "Discover tools that are not currently loaded into your context. Given a "
        "natural-language capability query, returns the most relevant tool names "
        "and descriptions. To actually use a returned tool, name it explicitly in "
        "your next plan or output so its full schema is injected on the next turn."
    )
    args_schema: Type[BaseModel] = ToolSearchInput

    _store: ToolRAGStore = PrivateAttr()

    def __init__(self, *, store: Optional[ToolRAGStore] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Default to the module singleton; tests inject an isolated store.
        self._store = store if store is not None else tool_rag_store

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ToolSearchTool is async-only — use _arun().")

    async def _arun(
        self,
        query: str,
        k: int = TOOL_RAG_TOP_K,
        config: Optional[RunnableConfig] = None,
    ) -> str:
        active_role = _resolve_active_role(config)
        session_mode = _resolve_session_mode(config)
        matches: List[ToolSchema] = await self._store.select_tools(
            query,
            k=min(k, TOOL_RAG_TOP_K),
            active_role=active_role,
            session_mode=session_mode,
        )
        logger.info(
            "tool_search: role=%s mode=%s query=%r -> %d match(es)",
            active_role,
            session_mode.value,
            query,
            len(matches),
        )
        if not matches:
            return (
                "[tool_search] No tools matched your query. Refine the capability "
                "you need and try again."
            )
        listing = json.dumps(
            [{"name": m.name, "description": m.description} for m in matches]
        )
        return (
            "[tool_search] These tools exist but are NOT yet loaded into your "
            "context. To use one, name it explicitly in your next plan/output so its "
            "full schema is injected on the next execution cycle:\n" + listing
        )


# =====================================================================
# Schema registration
# =====================================================================


def _meta_schema(
    name: str, description: str, input_model: Type[BaseModel]
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=_CONTROL_ROLES,
    )


async def register_meta_tools(store: ToolRAGStore) -> int:
    """Register the universal meta-tool schema(s) in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _meta_schema(
            "tool_search",
            "Retrieve relevant tool schemas by capability query when tools are "
            "deferred from the prompt; returns names and descriptions to load next.",
            ToolSearchInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
