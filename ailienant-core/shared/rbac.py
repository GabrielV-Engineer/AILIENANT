# ailienant-core/shared/rbac.py

from enum import Enum
from pydantic import BaseModel, Field
from typing import Dict, FrozenSet, Iterable, List


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
DEV_ROLES: FrozenSet[str] = frozenset(
    {
        "core_dev",
        "architect_refactor",
        "devops_infra",
        "secops",
        "qa_tester",
        "doc_manager",
        "vcs_manager",
        "data_ml_engineer",
    }
)
"""The 8 canonical developer roles — the single source every other role set derives
its dev half from. ``agents.roles.ROLE_REGISTRY`` owns each role's prompt and gates;
its keys must equal this set, which ``assert_role_registry_parity`` mechanizes."""


CRITIC_ROLE: str = "analyst_readonly"
"""The adversarial review identity a dispatched critic runs under. Not a developer
role: it is floor-locked to READ_ONLY below and must never reach a mutating tool."""


COGNITIVE_ROLES: FrozenSet[str] = frozenset(
    {"researcher", "analyst", "planner", "orchestrator"}
)
"""The graph-node roles. Distinct from the dev roles: each is one node in the
cognitive pipeline rather than a mode the CoderAgent runs in."""


ALL_ROLES: FrozenSet[str] = DEV_ROLES | COGNITIVE_ROLES | {CRITIC_ROLE}
"""The complete role universe — every identity that can appear as a dispatcher's
``active_role``. Tools visible to everyone (tool discovery, the TODO scratchpad)
register with this set."""


DISPATCH_ROLE_PERMISSIONS: Dict[str, PermissionMode] = {
    **{role: PermissionMode.EDIT_EXECUTE_RBW for role in DEV_ROLES},
    CRITIC_ROLE: PermissionMode.READ_ONLY,
}


def assert_role_registry_parity(registry_roles: Iterable[str]) -> None:
    """Fail loudly when ``ROLE_REGISTRY`` and ``DEV_ROLES`` disagree.

    The registry owns behaviour (prompts, forbidden phrases, HITL triggers) and is
    deliberately not derived from this module; only the agreement between the two
    is mechanized, so a role added to one and forgotten in the other cannot ship as
    a silently tool-less or permission-less identity.
    """
    registry = frozenset(registry_roles)
    if registry != DEV_ROLES:
        missing = sorted(DEV_ROLES - registry)
        extra = sorted(registry - DEV_ROLES)
        raise RuntimeError(
            f"role registry drift: missing from ROLE_REGISTRY={missing}, "
            f"missing from DEV_ROLES={extra}"
        )


def resolve_dispatch_permission(role: str) -> PermissionMode:
    """Map a dispatch subagent role to its permission floor (READ_ONLY if unknown)."""
    return DISPATCH_ROLE_PERMISSIONS.get(role, PermissionMode.READ_ONLY)
