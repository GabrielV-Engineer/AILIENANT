"""Gateway-surface tool wrappers.

Thin typed BaseTool subclasses exposing the capability catalog, skill resolver, and
background-task management surface as RBAC-gated LangChain tools. They call the same
substrate functions as the external gateway verbs — no duplicated runner logic.

The benchmark pair lived here too and was removed: gateway/handlers.py owns that
surface, submitting over loopback to the running host so the host's own single-flight
and task lifecycle apply. The copy here dispatched in-process and had to re-implement
both, which is the duplication this module's contract forbids.

All imports from the substrate (gateway.catalog, skill_resolver) are deferred to
_arun to keep module-load cheap and avoid circular-import risks.
"""
from __future__ import annotations

import json
import logging
from typing import Any, FrozenSet, List, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.execution_tools import TASK_CREATE_ROLES, BackgroundTaskManager

logger = logging.getLogger("GATEWAY_TOOLS")


# =====================================================================
# Role sets
# =====================================================================

_CATALOG_ROLES: FrozenSet[str] = frozenset({"orchestrator", "planner"})
_SKILL_ROLES: FrozenSet[str] = frozenset({"orchestrator", "planner"})
# task_list / task_stop reach every role that can create a task. Spawning a
# background task without being able to list or kill one is an asymmetry, not a
# privilege boundary: a hung task then has no cleanup path. Derived from
# execution_tools' own creator set so the two halves cannot drift; per-role
# visibility is still enforced inside the tools by `owner_role`, which only the
# orchestrator may exceed.
_TASK_MGR_ROLES: FrozenSet[str] = TASK_CREATE_ROLES | frozenset({"orchestrator"})


# =====================================================================
# C — ListCapabilitiesTool
# =====================================================================


class ListCapabilitiesInput(BaseModel):
    include_deprecated: bool = Field(
        default=False,
        description="When true, include capabilities marked as deprecated in the result.",
    )


class ListCapabilitiesTool(BaseTool):
    """List all capabilities exposed by the AILIENANT external gateway."""

    name: str = "list_capabilities"
    description: str = (
        "Return a JSON array of the capabilities the gateway exposes "
        "(name, description, privilege tier, async flag). "
        "Deprecated capabilities are excluded by default."
    )
    args_schema: Type[BaseModel] = ListCapabilitiesInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ListCapabilitiesTool is async-only — use _arun().")

    async def _arun(self, include_deprecated: bool = False) -> str:
        from gateway.catalog import CATALOG  # deferred — mcp package required at runtime

        caps = [
            {
                "name": c.name,
                "description": c.description,
                "tier": c.tier.value,
                "async": c.is_async,
            }
            for c in CATALOG
            if include_deprecated or not c.deprecated
        ]
        return json.dumps(caps)


# =====================================================================
# D — SkillInvokeTool
# =====================================================================


class SkillInvokeInput(BaseModel):
    user_input: str = Field(description="Task or query to match skills against.")
    workspace_root: str = Field(description="Workspace root for scope-filtering skills.")
    skill_id: Optional[str] = Field(
        default=None,
        description="Optional explicit skill ID to invoke directly (bypasses matching).",
    )


class SkillInvokeTool(BaseTool):
    """Resolve and return skills relevant to a task description.

    Omits embed_fn so resolve_active_skills falls back to its own default embedder
    (the shared LiteLLM proxy) — semantic auto-matching is live. An embedding-provider
    outage degrades to explicit-only, per the resolver's own guaranteed behavior;
    explicit skill_id invocation is unaffected either way (DB exact-lookup path).
    """

    name: str = "skill_invoke"
    description: str = (
        "Resolve skills relevant to the given task. Invoke a specific skill by "
        "skill_id, or omit skill_id for semantic auto-matching against each "
        "candidate skill's description. Returns a JSON list capped at 20."
    )
    args_schema: Type[BaseModel] = SkillInvokeInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SkillInvokeTool is async-only — use _arun().")

    async def _arun(
        self, user_input: str, workspace_root: str, skill_id: Optional[str] = None
    ) -> str:
        if not workspace_root.strip():
            return "[skill_invoke] REJECTED: workspace_root is required"

        from core.skill_resolver import resolve_active_skills  # deferred

        skills = await resolve_active_skills(
            user_input=user_input,
            workspace_root=workspace_root,
            invoked_skill_id=skill_id,
        )
        skills_capped = skills[:20]
        return json.dumps({"count": len(skills), "skills": skills_capped})


# =====================================================================
# E — TaskListTool
# =====================================================================


class TaskListInput(BaseModel):
    status_filter: Optional[str] = Field(
        default=None,
        description=(
            "Filter by task status: 'running', 'completed', 'failed', 'cancelled'. "
            "Omit to return all tasks."
        ),
    )
    caller_role: Optional[str] = Field(
        default=None,
        description=(
            "Role of the calling agent. Non-orchestrator roles see only tasks they created "
            "(owner_role matches). Orchestrator sees all tasks. Omit for full visibility."
        ),
    )


class TaskListTool(BaseTool):
    """List background tasks registered with the BackgroundTaskManager.

    Returns a snapshot of task metadata; raw stdout/stderr output is excluded for
    token hygiene (§5.5). Capped at 50 entries.
    """

    name: str = "task_list"
    description: str = (
        "List all background tasks (spawned via task_create) with their status and "
        "metadata. Raw output is excluded. Results are capped at 50 entries."
    )
    args_schema: Type[BaseModel] = TaskListInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskListTool is async-only — use _arun().")

    async def _arun(self, status_filter: Optional[str] = None, caller_role: Optional[str] = None) -> str:
        tasks = self._manager.list_tasks(caller_role=caller_role)
        if status_filter is not None:
            tasks = {k: v for k, v in tasks.items() if v.get("status") == status_filter}
        total = len(tasks)
        capped = dict(list(tasks.items())[:50])
        return json.dumps({"count": total, "tasks": capped, "truncated": total > 50})


# =====================================================================
# F — TaskStopTool
# =====================================================================


class TaskStopInput(BaseModel):
    task_id: str = Field(description="UUID returned by a prior task_create call.")


class TaskStopTool(BaseTool):
    """Terminate a running background task (soft signal, grace, then force-kill).

    Returns 'cancelled' if found and terminated, 'not_found_or_completed' if the
    task was already done or the ID is unknown.
    """

    name: str = "task_stop"
    description: str = (
        "Terminate a background task spawned via task_create (SIGTERM, then SIGKILL "
        "after a grace period). Returns 'cancelled' on success or "
        "'not_found_or_completed' if already done."
    )
    args_schema: Type[BaseModel] = TaskStopInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _manager: BackgroundTaskManager = PrivateAttr()

    def __init__(self, *, manager: BackgroundTaskManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskStopTool is async-only — use _arun().")

    async def _arun(self, task_id: str) -> str:
        stopped = await self._manager.stop(task_id)
        if not stopped:
            return json.dumps({"status": "not_found_or_completed", "task_id": task_id})
        return json.dumps({"status": "cancelled", "task_id": task_id})


# =====================================================================
# Schema registration
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    *,
    tier: ToolPrivilegeTier,
    roles: FrozenSet[str],
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=tier,
        allowed_roles=roles,
    )


async def register_gateway_tools(store: ToolRAGStore) -> int:
    """Register the 4 gateway-surface schemas. Returns count (4)."""
    schemas: List[ToolSchema] = [
        _tool_schema(
            "list_capabilities",
            "Return JSON array of gateway capabilities (name, description, tier, async).",
            ListCapabilitiesInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_CATALOG_ROLES,
        ),
        _tool_schema(
            "skill_invoke",
            "Resolve skills for a task by explicit ID or auto-match; returns list capped at 20.",
            SkillInvokeInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_SKILL_ROLES,
        ),
        _tool_schema(
            "task_list",
            "List background tasks (status + metadata, no raw output). Cap 50 entries.",
            TaskListInput,
            tier=ToolPrivilegeTier.READ_ONLY,
            roles=_TASK_MGR_ROLES,
        ),
        _tool_schema(
            "task_stop",
            "Send SIGTERM to a running background task; returns cancellation status.",
            TaskStopInput,
            tier=ToolPrivilegeTier.EXECUTE,
            roles=_TASK_MGR_ROLES,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
