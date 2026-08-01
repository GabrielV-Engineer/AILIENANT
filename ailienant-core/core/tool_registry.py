# ailienant-core/core/tool_registry.py
"""The missing name → callable bridge — Division 8.18.

``core/tool_rag.py::ToolRAGStore`` holds ``ToolSchema`` metadata (name,
description, tier, allowed roles); ``core/tool_dispatch.py::ToolDispatcher``
executes a ``Dict[str, RegisteredTool]`` of live tool instances. Nothing
previously connected the two — this is the component ``agents/roles.py``'s
``RoleConfig`` docstring still calls the "Phase 5 MCP executor," which was
never built. ``agents/roles.py::allowed_tools`` is superseded by
``ToolSchema.allowed_roles`` as the single RBAC source of truth: the former is
never read by any dispatch path (verified by exhaustive grep), the latter is
consulted by ``ToolDispatcher`` on every call.

``resolve_tools()`` reuses each tool family's own existing
``build_*_tools(state) -> Dict[str, RegisteredTool]`` factory
(``build_analyst_tools``, ``build_researcher_tools``, ``build_perception_tools``)
wherever one already exists, and wraps the remaining simple, state-bound, or
list-returning factories using the tier/roles carried on the *selected*
``ToolSchema`` — never a duplicated, hand-maintained role constant. A
selected schema with neither an existing factory nor an entry in
``_INTENTIONALLY_UNREGISTERED`` raises immediately; it is never silently
dropped. The Division 8.18.4 checkpoint gate asserts every schema the store
can register falls into one of those two buckets, so this class of gap
cannot silently recur.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, MutableMapping, Sequence

from langchain_core.tools import BaseTool

from core.tool_dispatch import RegisteredTool
from core.tool_rag import ToolSchema

# =====================================================================
# Intentionally unregistered — documented, reasoned exclusions, checked by
# the 8.18.4 reachability gate. Not accidental gaps: each of these either
# duplicates a capability already live elsewhere, or was already excluded by
# its own module's docstring for a reason specific to that tool.
# =====================================================================

_INTENTIONALLY_UNREGISTERED: Dict[str, str] = {
    "atomic_code_patch": (
        "redundant with brain/agentic_cell.py's apply_granular_edit primitive "
        "(SEARCH/REPLACE under an OCC guard); no safe vfs_write(path, content) "
        "closure exists in production — writes flow through "
        "VFSMiddleware.ingest_dirty_buffers, not a simple write API"
    ),
    "batch_semantic_edit": (
        "redundant with brain/agentic_cell.py's apply_granular_edit primitive; "
        "no safe vfs_write(path, content) closure exists in production"
    ),
    "file_write": (
        "redundant with brain/agentic_cell.py's apply_granular_edit primitive; "
        "no safe vfs_write(path, content) closure exists in production"
    ),
    "generate_docstring": (
        "redundant with brain/agentic_cell.py's apply_granular_edit primitive — "
        "docstring insertion is a strict subset of its SEARCH/REPLACE capability; "
        "no safe vfs_write(path, content) closure exists in production"
    ),
    "guard_env_file": (
        "excluded by tools/coder_tools.py::make_coder_execute_tools's own "
        "docstring: it already emits its own content-hash-idempotent HITL gate "
        "and must not be double-gated by a generic dispatch wrapper"
    ),
    "run_benchmark": (
        "duplicates gateway/handlers.py's real MCP-gateway logic; canonical "
        "owner is the gateway package (a standalone process), not this "
        "in-process registry"
    ),
    "get_benchmark_report": (
        "duplicates gateway/handlers.py's real MCP-gateway logic; canonical "
        "owner is the gateway package (a standalone process), not this "
        "in-process registry"
    ),
    # The following four are scoped to orchestrator and/or planner only (see
    # each tool's allowed_roles), which is disjoint from resolve_tools()'s only
    # runtime consumer: brain/agentic_cell.py resolves the active role from the
    # WBS step's target_role, always one of the 8 canonical coder roles.
    # Neither orchestrator nor planner runs a ToolDispatcher loop (a permanent
    # architectural decision — the orchestrator is a deterministic node with no
    # reasoner to drive a loop, and the planner is PLAN-only; see the DEBT-068
    # scope correction), so a factory here would build a tool no reachable role
    # is ever permitted to call. NOT a gateway duplicate — gateway/catalog.py's
    # CATALOG carries no skill/task-list/task-stop/capability-listing entry.
    "list_capabilities": (
        "scoped to {orchestrator, planner}; disjoint from the coder roles "
        "resolve_tools() serves, and neither role runs a dispatch loop"
    ),
    "skill_invoke": (
        "scoped to {orchestrator, planner}; disjoint from the coder roles "
        "resolve_tools() serves, and neither role runs a dispatch loop"
    ),
    "task_list": (
        "scoped to {orchestrator} only; disjoint from the coder roles "
        "resolve_tools() serves, and orchestrator runs no dispatch loop"
    ),
    "task_stop": (
        "scoped to {orchestrator} only; disjoint from the coder roles "
        "resolve_tools() serves, and orchestrator runs no dispatch loop"
    ),
}


ToolFactory = Callable[[MutableMapping[str, Any]], BaseTool]


def _simple_factories() -> Dict[str, ToolFactory]:
    """Bare-constructor and state-bound tools with no existing dict factory.

    Built lazily (called once per ``resolve_tools`` invocation, not at import)
    so importing this module never imports the whole tool surface eagerly.
    """
    from tools.agent_tools import (
        make_ask_user_question_tool,
        make_state_aware_read_file_tool,
        make_toggle_plan_mode_tool,
    )
    from tools.coder_tools import ASTValidateTool, SecurityAuditTool
    from tools.execution_tools import CheckTypeIntegrityTool, SandboxBashTool
    from tools.meta_tools import ToolSearchTool
    from tools.planner_tools import BudgetEstimatorTool, ValidateWBSDependenciesTool
    from tools.universal_tools import TodoWriteTool
    from core.vfs_middleware import make_safe_reader

    def _read_file(state: MutableMapping[str, Any]) -> BaseTool:
        project_id = str(state.get("project_id") or "") or None
        workspace_root = str(state.get("workspace_root") or "") or None
        session_id_raw = state.get("session_id")
        session_id = str(session_id_raw) if session_id_raw else None
        vfs_read = make_safe_reader(project_id, workspace_root, session_id)
        return make_state_aware_read_file_tool(state, vfs_read)

    return {
        "security_audit": lambda _state: SecurityAuditTool(),
        "validate_ast": lambda _state: ASTValidateTool(),
        "sandbox_bash": lambda _state: SandboxBashTool(),
        "check_type_integrity": lambda _state: CheckTypeIntegrityTool(),
        "todo_write": lambda _state: TodoWriteTool(),
        "tool_search": lambda _state: ToolSearchTool(),
        "validate_wbs_dependencies": lambda state: ValidateWBSDependenciesTool(state=state),
        "estimate_plan_budget": lambda state: BudgetEstimatorTool(state=state),
        "ask_user_question": lambda state: make_ask_user_question_tool(state),
        "toggle_plan_mode": lambda state: make_toggle_plan_mode_tool(state),
        "read_file": _read_file,
    }


def _build_task_tools(state: MutableMapping[str, Any]) -> Dict[str, BaseTool]:
    """``task_create`` + ``task_get`` bound to ONE shared ``BackgroundTaskManager``.

    ``tools/agent_tools.py::make_task_create_tool``/``make_task_get_tool`` each
    independently call ``state.setdefault("background_tasks", {})`` and wrap it
    in a *new* ``BackgroundTaskManager`` — same underlying dict, different
    manager objects, so a task started via one manager's in-process
    watcher/subprocess handle is invisible to the other. Constructing one
    manager here and injecting it into both tools closes that gap within this
    registry's scope (``BackgroundTaskManager`` itself is unchanged).
    """
    from tools.execution_tools import BackgroundTaskManager, TaskCreateTool, TaskGetTool

    registry = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    return {
        "task_create": TaskCreateTool(manager=manager),
        "task_get": TaskGetTool(manager=manager),
    }


def _wrap_with_schema(
    tools: Sequence[BaseTool], by_name: Dict[str, ToolSchema]
) -> Dict[str, RegisteredTool]:
    """Pair bare ``BaseTool`` instances with their registered schema's tier/roles.

    Silently skips a tool whose name isn't in ``by_name`` — it means that name
    wasn't part of the caller's selection for this turn, not that it's unknown.
    """
    out: Dict[str, RegisteredTool] = {}
    for tool in tools:
        schema = by_name.get(tool.name)
        if schema is None:
            continue
        out[tool.name] = RegisteredTool(tool, schema.privilege_tier, schema.allowed_roles)
    return out


def resolve_tools(
    schemas: Sequence[ToolSchema], state: MutableMapping[str, Any]
) -> Dict[str, RegisteredTool]:
    """Resolve selected ``ToolSchema`` entries to live, dispatch-ready tools.

    Reuses each tool family's own state-bound factory wherever one exists
    (``build_analyst_tools``, ``build_researcher_tools``, and
    ``build_perception_tools`` already return ``RegisteredTool`` paired with
    tier/roles); wraps the remaining simple/list-returning factories using the
    *selected* schema's own tier/roles. A schema name that resolves to neither
    an existing factory nor ``_INTENTIONALLY_UNREGISTERED`` raises — an
    unrecognized gap must fail loudly, never disappear silently.
    """
    from tools.agent_tools import build_orchestrator_tools
    from tools.analyst_tools import build_analyst_tools
    from tools.coder_tools import make_coder_execute_tools
    from tools.perception_tools import build_perception_tools
    from tools.researcher_tools import build_researcher_tools

    by_name: Dict[str, ToolSchema] = {s.name: s for s in schemas}
    wanted = set(by_name)
    if not wanted:
        return {}

    resolved: Dict[str, RegisteredTool] = {}

    # 1. Delegate to existing Dict[str, RegisteredTool] factories wholesale —
    #    each is already cheap/lazy per its own docstring, so building the
    #    full family and filtering down is the same cost profile these
    #    factories already have in their existing (analyst/researcher) callers.
    resolved.update(build_analyst_tools(state))
    resolved.update(build_researcher_tools(state))
    resolved.update(build_perception_tools(state))

    # 2. List[BaseTool]-returning factories — pair with the selected schema's
    #    own tier/roles rather than re-deriving/duplicating them here.
    resolved.update(_wrap_with_schema(make_coder_execute_tools(state), by_name))
    resolved.update(_wrap_with_schema(build_orchestrator_tools(state), by_name))

    # 3. Shared-dependency cluster (task manager) + simple/state-bound singles.
    resolved.update(_wrap_with_schema(list(_build_task_tools(state).values()), by_name))
    for name, factory in _simple_factories().items():
        if name in wanted and name not in resolved:
            schema = by_name[name]
            resolved[name] = RegisteredTool(factory(state), schema.privilege_tier, schema.allowed_roles)

    # 4. Every requested name must now be resolved or explicitly excluded.
    missing = [n for n in wanted if n not in resolved and n not in _INTENTIONALLY_UNREGISTERED]
    if missing:
        raise KeyError(
            f"core.tool_registry: no factory registered for tool(s) {missing!r} — "
            "either add a factory in resolve_tools()/_simple_factories(), or add "
            "a reasoned entry to _INTENTIONALLY_UNREGISTERED if this is deliberate."
        )

    return {name: resolved[name] for name in wanted if name in resolved}


def all_registrable_names() -> Dict[str, str]:
    """Every name this module claims to resolve, minus the unregistered set.

    Used only by the 8.18.4 checkpoint gate to assert full-catalog coverage
    without constructing every tool (which would require a live ``state``).
    Returns ``{name: "factory" | "delegated"}`` — the value is diagnostic only.
    """
    delegated = {
        "run_linter", "analyze_complexity", "diff_changes", "audit_dependencies",
        "web_search", "read_token_ledger", "detect_dead_code", "architecture_digest",
        "find_symbol_callers", "trace_cross_boundary",  # build_analyst_tools
        "glob", "grep", "workspace_structure", "query_graphrag", "get_dependents",
        # architecture_digest/find_symbol_callers/trace_cross_boundary already listed
        "document_parser", "inspect_ast_node", "get_symbol_references",
        "trace_data_flow", "web_fetch",  # build_perception_tools
        "run_tests", "git_stage", "git_commit", "git_diff", "linter_autofix",
        "install_dependency", "run_data_pipeline",  # make_coder_execute_tools
        "get_wbs_status", "emit_hitl_request",  # build_orchestrator_tools
        "task_create", "task_get",
    }
    return {**{n: "delegated" for n in delegated}, **{n: "factory" for n in _simple_factories()}}
