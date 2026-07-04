# ailienant-core/shared/rbac.py

from enum import Enum
from pydantic import BaseModel, Field
from typing import Dict, List


class PermissionMode(str, Enum):
    """Strict Role-Based Access Control (RBAC) for the cognitive nodes."""

    PLAN_ONLY = "plan_only"  # May only generate a WBS (Planner).
    ROUTING_ONLY = "routing_only"  # May only decide which node to go to (Orchestrator).
    EDIT_EXECUTE_RBW = "edit_execute_rbw"  # May modify code with Read-Before-Write (Logic).
    READ_ONLY = "read_only"  # Analyzes, but never touches the VFS (Analyst).


class AgentIdentity(BaseModel):
    """Immutable identity contract for a node."""

    name: str = Field(..., description="Power-node name")
    role_description: str = Field(..., description="The base system prompt")
    permission_mode: PermissionMode
    allowed_tools: List[str] = Field(
        default_factory=list, description="Authorized MCP tools"
    )


# Power instances (our 4 base nodes)
PLANNER_IDENTITY = AgentIdentity(
    name="PlannerAgent",
    role_description="You are the Strategist. You transform requirements into an immutable WBS.",
    permission_mode=PermissionMode.PLAN_ONLY,
    allowed_tools=[],  # No execution tools
)

LOGIC_IDENTITY = AgentIdentity(
    name="LogicAgent",
    role_description="You are the Builder. You execute the WBS steps by modifying the code.",
    permission_mode=PermissionMode.EDIT_EXECUTE_RBW,
    allowed_tools=["edit_file", "run_terminal"],
)

# ResearcherAgent (The Context Hound).
# Strictly read-only: explores GraphRAG + @-mention bypass to emit a Skeleton Map
# for the PlannerAgent. Tools are programmatic (Python); LangChain bind_tools /
# ReAct is deferred until the CoderAgent transmutation.
RESEARCHER_IDENTITY = AgentIdentity(
    name="ResearcherAgent",
    role_description=(
        "You are the Context Hound. Strictly read-only: explore GraphRAG and, "
        "when the user supplies @-mentions, the requested files verbatim to build "
        "a Skeleton Map (function signatures, class headers, cross-module relations, "
        "and file paths) that the PlannerAgent will consume next. Forbidden: writing "
        "code, returning full file dumps, or proposing implementations."
    ),
    permission_mode=PermissionMode.READ_ONLY,
    allowed_tools=[],
)

# (Orchestrator and Analyst follow this same pattern.)


# ---------------------------------------------------------------------------
# Dynamic subagent dispatch — role → permission floor.
#
# A dispatched subagent resolves its RBAC identity through this map, exactly as an
# ordinary WBS step resolves one. The developer roles carry the write/execute-capable
# identity; the adversarial critic (analyst_readonly) is pinned to READ_ONLY so
# ``core.permissions.evaluate_action`` denies it any WRITE/EXECUTE/DANGEROUS tool in
# every session mode — it must never mutate what it judges. An unknown role resolves to
# the READ_ONLY floor (fail-safe: a subagent can never escalate past its map entry).
# ---------------------------------------------------------------------------
_DEV_ROLES = (
    "core_dev",
    "architect_refactor",
    "devops_infra",
    "secops",
    "qa_tester",
    "doc_manager",
    "vcs_manager",
    "data_ml_engineer",
)
DISPATCH_ROLE_PERMISSIONS: Dict[str, PermissionMode] = {
    **{role: PermissionMode.EDIT_EXECUTE_RBW for role in _DEV_ROLES},
    "analyst_readonly": PermissionMode.READ_ONLY,
}


def resolve_dispatch_permission(role: str) -> PermissionMode:
    """Map a dispatch subagent role to its permission floor (READ_ONLY if unknown)."""
    return DISPATCH_ROLE_PERMISSIONS.get(role, PermissionMode.READ_ONLY)
