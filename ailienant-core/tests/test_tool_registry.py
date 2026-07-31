"""Division 8.18.0 — core/tool_registry.py, the name -> callable bridge.

Covers three contracts:
  1. Full-catalog coverage — every schema name any of the 12 register_*_tools
     functions can register resolves via resolve_tools() or is explicitly
     listed in _INTENTIONALLY_UNREGISTERED with a reason. No accidental gap.
  2. Construction — every factory-covered name actually constructs a live
     BaseTool without raising, paired with its schema's own tier/roles.
  3. Failure mode — an unrecognized name (neither factory nor allowlisted)
     raises KeyError immediately rather than being silently dropped.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema, populate_tool_catalog
from core.tool_registry import (
    _INTENTIONALLY_UNREGISTERED,
    all_registrable_names,
    resolve_tools,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


async def _full_catalog_names() -> List[str]:
    """Populate an isolated store via the real startup path; return unique names."""
    store = ToolRAGStore(embed_fn=_fake_embed)
    await populate_tool_catalog(store)
    return sorted({s.name for s in store.all_schemas()})


async def test_populate_tool_catalog_is_idempotent() -> None:
    store = ToolRAGStore(embed_fn=_fake_embed)
    first = await populate_tool_catalog(store)
    names_after_first = sorted({s.name for s in store.all_schemas()})

    second = await populate_tool_catalog(store)
    names_after_second = sorted({s.name for s in store.all_schemas()})

    assert first == second, "a second population pass registered a different count"
    assert names_after_first == names_after_second, "re-registration added/lost rows"
    assert len(names_after_second) == len(set(names_after_second)), "duplicate rows after re-registration"


async def test_full_catalog_is_covered_with_no_gap() -> None:
    names = await _full_catalog_names()
    covered = set(all_registrable_names()) | set(_INTENTIONALLY_UNREGISTERED)

    missing = [n for n in names if n not in covered]
    assert not missing, (
        f"tool_registry has no factory AND no documented exclusion for {missing} — "
        "this is exactly the class of gap Division 8.18 exists to close."
    )
    # Informational, not load-bearing: catches a name removed from the live
    # catalog that the registry still claims to cover — drift in the other
    # direction. Not asserted strictly since a future tool family may
    # legitimately add names between catalog snapshots.
    extra = [n for n in covered if n not in names]
    assert extra == [], f"registry claims tools no longer in the catalog: {extra}"


def test_every_intentional_exclusion_has_a_reason() -> None:
    for name, reason in _INTENTIONALLY_UNREGISTERED.items():
        assert reason.strip(), f"{name} is unregistered with no documented reason"


def _schemas_for(names: List[str], role: str = "core_dev") -> List[ToolSchema]:
    return [
        ToolSchema(
            name=n,
            description="test",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({role}),
        )
        for n in names
    ]


def _base_state() -> Dict[str, Any]:
    return {
        "workspace_root": ".",
        "project_id": "test-project",
        "session_id": "session-1",
        "session_permission_mode": "DEFAULT",
        "mission_spec": None,
        "background_tasks": {},
    }


def test_every_factory_covered_name_constructs() -> None:
    names = list(all_registrable_names())
    schemas = _schemas_for(names)
    resolved = resolve_tools(schemas, _base_state())
    assert set(resolved) == set(names)
    for name, reg in resolved.items():
        assert reg.tool.name == name


# Names resolved via _simple_factories()/_build_task_tools()/make_coder_execute_tools()/
# build_orchestrator_tools() are paired with the *passed-in* schema's tier/roles (see
# resolve_tools's _wrap_with_schema step). Delegated dict-returning factories
# (build_analyst_tools/build_researcher_tools/build_perception_tools) return their own
# authoritative RegisteredTool and ignore the caller's schema — asserted separately
# in test_perception_tools_resolve_with_correct_per_tool_roles.
_SCHEMA_WRAPPED_NAMES: List[str] = [
    "security_audit", "validate_ast", "sandbox_bash", "check_type_integrity",
    "todo_write", "tool_search", "validate_wbs_dependencies", "estimate_plan_budget",
    "ask_user_question", "toggle_plan_mode", "read_file",
    "run_tests", "git_stage", "git_commit", "git_diff", "linter_autofix",
    "install_dependency", "run_data_pipeline",
    "get_wbs_status", "emit_hitl_request",
    "task_create", "task_get",
]


def test_schema_wrapped_names_inherit_the_passed_schemas_tier_and_roles() -> None:
    schemas = _schemas_for(_SCHEMA_WRAPPED_NAMES, role="qa_tester")
    resolved = resolve_tools(schemas, _base_state())
    assert set(resolved) == set(_SCHEMA_WRAPPED_NAMES)
    for name, reg in resolved.items():
        assert reg.tier == ToolPrivilegeTier.READ_ONLY, name
        assert reg.allowed_roles == frozenset({"qa_tester"}), name


def test_resolve_tools_empty_selection_is_empty_dict() -> None:
    assert resolve_tools([], _base_state()) == {}


def test_unrecognized_tool_name_raises_not_silently_dropped() -> None:
    schemas = _schemas_for(["definitely_not_a_real_tool_name"])
    with pytest.raises(KeyError):
        resolve_tools(schemas, _base_state())


def test_task_create_and_task_get_share_one_manager() -> None:
    schemas = _schemas_for(["task_create", "task_get"])
    state = _base_state()
    resolved = resolve_tools(schemas, state)

    create_manager = resolved["task_create"].tool._manager  # type: ignore[attr-defined]
    get_manager = resolved["task_get"].tool._manager  # type: ignore[attr-defined]
    assert create_manager is get_manager, (
        "task_create and task_get must share one BackgroundTaskManager instance — "
        "the two agent_tools.py factories independently build separate managers "
        "over the same dict, which loses in-process subprocess-handle visibility "
        "across the pair; resolve_tools() must close that gap, not reproduce it."
    )


def test_perception_tools_resolve_with_correct_per_tool_roles() -> None:
    """document_parser omits 'analyst' from its role set; web_fetch includes it —
    both are delegated to build_perception_tools(), which must preserve each
    tool's own role set rather than a single shared constant. The test schema's
    own allowed_roles is deliberately irrelevant here: delegated factories
    (analyst/researcher/perception) return their own authoritative RegisteredTool,
    ignoring the caller-supplied schema for tier/roles — only the non-delegated
    simple factories use the passed-in schema (see test_every_factory_covered_name_constructs)."""
    names = ["document_parser", "web_fetch"]
    schemas = _schemas_for(names, role="secops")
    resolved = resolve_tools(schemas, _base_state())
    assert set(resolved) == {"document_parser", "web_fetch"}
    assert "analyst" not in resolved["document_parser"].allowed_roles
    assert "analyst" in resolved["web_fetch"].allowed_roles
    assert "secops" in resolved["document_parser"].allowed_roles
    assert "secops" in resolved["web_fetch"].allowed_roles
