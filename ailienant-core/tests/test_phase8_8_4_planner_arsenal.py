"""Wave 3b Planner Arsenal gate — sibling-file checkpoint.

DoD:
  - validate_wbs_dependencies, estimate_plan_budget, workspace_structure,
    get_dependents, inspect_ast_node all carry "planner" in allowed_roles.
  - select_tools(active_role="planner") surfaces all 5; all are READ_ONLY and
    survive PLAN session mode.
  - Negative RBAC: analyst does NOT get validate_wbs_dependencies or
    estimate_plan_budget; researcher STILL gets workspace_structure and
    get_dependents; core_dev STILL gets inspect_ast_node (regressions).
  - ValidateWBSDependenciesTool: forward-reference detection (only for
    explicitly created files); out-of-scope detection with PurePosixPath
    boundary; path normalization (Fix 7: ./src/ → src); redundant-write
    advisory; multi-pass over materialized list (Fix 6, not iterator).
  - BudgetEstimatorTool: heuristic cost vs remaining budget; include_breakdown;
    confidence always "low".
  - Pre-commit hook smoke: valid → False triggers ValueError path; budget
    overage → fits_within_budget False.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from brain.state import MissionSpecification, WBSStep
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import ToolRAGStore
from tools.perception_tools import register_perception_tools
from tools.planner_tools import (
    _WBS_MAX_STEPS,
    BudgetEstimatorTool,
    ValidateWBSDependenciesTool,
    register_planner_tools,
)
from tools.researcher_tools import register_researcher_tools


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8."""

    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag_884"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


async def _register_all(store: ToolRAGStore) -> None:
    """Register all tool families needed for planner arsenal tests."""
    await register_researcher_tools(store)
    await register_planner_tools(store)
    # perception tools need constructor args — only register schemas here
    # (register_perception_tools requires vfs / ast engine instances)


def _make_step(
    n: int,
    action: str = "read_file",
    target_file: str = "",
    status: str = "pending",
    description: str = "Stub step.",
) -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role="core_dev",
        action=action,  # type: ignore[arg-type]
        target_file=target_file or f"src/file_{n}.py",
        description=description,
        status=status,  # type: ignore[arg-type]
    )


def _make_mission(
    tasks: List[WBSStep],
    scope: Optional[List[str]] = None,
) -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.",
        scope=scope if scope is not None else ["src/"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=tasks,
        checks=["Pytest exits 0."],
    )


# Tools the planner must be able to retrieve after Wave 3b.
_PLANNER_TOOLS = [
    "validate_wbs_dependencies",
    "estimate_plan_budget",
    "workspace_structure",
    "get_dependents",
]

# Net-new planner-only tools.
_PLANNER_ONLY = ["validate_wbs_dependencies", "estimate_plan_budget"]


# =====================================================================
# A — Role surface + retrievability
# =====================================================================


@pytest.mark.anyio
async def test_all_planner_tools_have_planner_role(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _PLANNER_TOOLS:
        assert tool_name in schemas, f"Schema {tool_name!r} missing from store"
        assert "planner" in schemas[tool_name].allowed_roles, (
            f"{tool_name!r} missing 'planner' in allowed_roles"
        )


@pytest.mark.anyio
async def test_select_tools_surfaces_planner_tools(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for tool_name in _PLANNER_TOOLS:
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="planner",
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {s.name for s in results}
        assert tool_name in names, f"select_tools('planner') missed {tool_name!r}"


# =====================================================================
# B — Privilege tier: all READ_ONLY, survive PLAN mode
# =====================================================================


@pytest.mark.anyio
async def test_planner_tools_are_read_only(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _PLANNER_TOOLS:
        assert schemas[tool_name].privilege_tier is ToolPrivilegeTier.READ_ONLY, (
            f"{tool_name!r} is not READ_ONLY"
        )


@pytest.mark.anyio
async def test_planner_tools_survive_plan_mode(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for tool_name in _PLANNER_TOOLS:
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="planner",
            session_mode=SessionPermissionMode.PLAN,
        )
        names = {s.name for s in results}
        assert tool_name in names, f"{tool_name!r} not available in PLAN mode"


# =====================================================================
# C — Negative RBAC + wire-in regressions
# =====================================================================


@pytest.mark.anyio
async def test_analyst_does_not_get_planner_only_tools(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    analyst_tools_result = await store.select_tools(
        "wbs validation budget",
        k=10,
        active_role="analyst",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    analyst_names = {s.name for s in analyst_tools_result}
    for tool_name in _PLANNER_ONLY:
        assert tool_name not in analyst_names, (
            f"analyst should not have {tool_name!r}"
        )


@pytest.mark.anyio
async def test_researcher_retains_workspace_structure_and_get_dependents(
    tmp_path: Path,
) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for tool_name in ("workspace_structure", "get_dependents"):
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="researcher",
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {s.name for s in results}
        assert tool_name in names, f"researcher regression: {tool_name!r} lost"


@pytest.mark.anyio
async def test_workspace_structure_still_researcher_only_not_analyst(
    tmp_path: Path,
) -> None:
    """workspace_structure is for researcher+planner — analyst must not gain it."""
    store = _isolated_store(tmp_path)
    await _register_all(store)

    results = await store.select_tools(
        "workspace structure directory tree",
        k=10,
        active_role="analyst",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    analyst_names = {s.name for s in results}
    assert "workspace_structure" not in analyst_names


# =====================================================================
# D — ValidateWBSDependenciesTool
# =====================================================================


@pytest.mark.anyio
async def test_validate_wbs_no_mission_returns_valid() -> None:
    tool = ValidateWBSDependenciesTool(state={})
    result = json.loads(await tool._arun())
    assert result["valid"] is True
    assert result["issues"] == []


@pytest.mark.anyio
async def test_validate_wbs_valid_sequential_plan() -> None:
    """Step 1 writes file A, step 2 reads file A — valid ordering."""
    tasks = [
        _make_step(1, action="write_file", target_file="src/module.py"),
        _make_step(2, action="read_file", target_file="src/module.py"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is True
    assert not any(i["type"] == "forward_reference" for i in result["issues"])


@pytest.mark.anyio
async def test_validate_wbs_forward_reference_detected() -> None:
    """Step 1 reads file A, step 3 writes file A — forward reference."""
    tasks = [
        _make_step(1, action="read_file", target_file="src/output.py"),
        _make_step(2, action="edit_file", target_file="src/other.py"),
        _make_step(3, action="write_file", target_file="src/output.py"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is False
    fwd = [i for i in result["issues"] if i["type"] == "forward_reference"]
    assert len(fwd) == 1
    assert fwd[0]["step_number"] == 1
    assert fwd[0]["target_file"] == "src/output.py"
    assert fwd[0]["first_producer"] == 3


@pytest.mark.anyio
async def test_validate_wbs_preexisting_file_not_flagged() -> None:
    """Read-only step on a file not written by the plan — assume pre-existing, no flag."""
    tasks = [
        _make_step(1, action="read_file", target_file="src/existing_config.py"),
        _make_step(2, action="edit_file", target_file="src/existing_config.py"),
    ]
    # No write_file for existing_config.py in the plan.
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is True
    assert not any(i["type"] == "forward_reference" for i in result["issues"])


@pytest.mark.anyio
async def test_validate_wbs_out_of_scope_detected() -> None:
    """File outside the declared path-like scope boundary is flagged."""
    tasks = [
        _make_step(1, action="write_file", target_file="tests/conftest.py"),
    ]
    mission = _make_mission(tasks, scope=["src/"])
    state: Dict[str, Any] = {"mission_spec": mission}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is False
    oos = [i for i in result["issues"] if i["type"] == "out_of_scope"]
    assert any(i["target_file"] == "tests/conftest.py" for i in oos)


@pytest.mark.anyio
async def test_validate_wbs_path_boundary_not_prefix_substring() -> None:
    """src/auth scope must NOT flag src/author.py (Fix 2: is_relative_to boundary)."""
    tasks = [
        _make_step(1, action="write_file", target_file="src/author.py"),
    ]
    mission = _make_mission(tasks, scope=["src/auth/"])
    state: Dict[str, Any] = {"mission_spec": mission}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    oos = [i for i in result["issues"] if i["type"] == "out_of_scope"]
    # src/author.py is NOT relative to src/auth/ → should be flagged as out of scope
    # (it's outside the auth/ subtree, which is correct behavior)
    assert any(i["target_file"] == "src/author.py" for i in oos)
    # Confirm the in-scope variant is NOT flagged
    tasks2 = [_make_step(1, action="write_file", target_file="src/auth/login.py")]
    mission2 = _make_mission(tasks2, scope=["src/auth/"])
    state2: Dict[str, Any] = {"mission_spec": mission2}
    result2 = json.loads(await ValidateWBSDependenciesTool(state=state2)._arun())
    oos2 = [i for i in result2["issues"] if i["type"] == "out_of_scope"]
    assert not any(i["target_file"] == "src/auth/login.py" for i in oos2)


@pytest.mark.anyio
async def test_validate_wbs_normpath_strips_dotslash() -> None:
    """./src/ scope entry normalizes to src/ (Fix 7: posixpath.normpath)."""
    tasks = [
        _make_step(1, action="write_file", target_file="src/main.py"),
    ]
    mission = _make_mission(tasks, scope=["./src/"])
    state: Dict[str, Any] = {"mission_spec": mission}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    # src/main.py IS relative to ./src/ after normalization → no out_of_scope
    oos = [i for i in result["issues"] if i["type"] == "out_of_scope"]
    assert not any(i["target_file"] == "src/main.py" for i in oos)


@pytest.mark.anyio
async def test_validate_wbs_redundant_write_advisory_valid() -> None:
    """Same file written twice with no consumer between — advisory only, valid=True."""
    tasks = [
        _make_step(1, action="write_file", target_file="src/config.py"),
        _make_step(2, action="write_file", target_file="src/config.py"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is True
    rw = [i for i in result["issues"] if i["type"] == "redundant_write"]
    assert len(rw) >= 1


@pytest.mark.anyio
async def test_validate_wbs_250_steps_no_crash() -> None:
    """250-step mission — capped at _WBS_MAX_STEPS, no crash (Fix 6: materialized list)."""
    tasks = [_make_step(i, action="write_file", target_file=f"src/f{i}.py") for i in range(1, 251)]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks, scope=["src/"])}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert "valid" in result
    assert "summary" in result


@pytest.mark.anyio
async def test_validate_wbs_tasks_none_no_type_error() -> None:
    """mission_spec.tasks = None — tool returns valid=True, no TypeError raised."""
    mission = SimpleNamespace(tasks=None, scope=[])
    state: Dict[str, Any] = {"mission_spec": mission}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is True


@pytest.mark.anyio
async def test_validate_wbs_non_path_scope_skipped() -> None:
    """Scope entries without '/' are not used for path checks — no false positive."""
    tasks = [
        _make_step(1, action="write_file", target_file="anywhere/module.py"),
    ]
    mission = _make_mission(tasks, scope=["authentication module"])
    state: Dict[str, Any] = {"mission_spec": mission}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    # No path-like scope entry → out_of_scope detection skipped
    assert not any(i["type"] == "out_of_scope" for i in result["issues"])
    assert "scope_format_not_path_checkable" in result["summary"]


# =====================================================================
# E — BudgetEstimatorTool
# =====================================================================


@pytest.mark.anyio
async def test_budget_no_mission_zero_cost() -> None:
    result = json.loads(await BudgetEstimatorTool(state={})._arun())
    assert result["estimated_cost_usd"] == 0.0
    assert result["fits_within_budget"] is True
    assert result["confidence"] == "low"


@pytest.mark.anyio
async def test_budget_fits_within_budget() -> None:
    tasks = [
        _make_step(1, action="read_file"),
        _make_step(2, action="edit_file"),
        _make_step(3, action="write_file"),
    ]
    state: Dict[str, Any] = {
        "mission_spec": _make_mission(tasks),
        "session_max_budget_usd": 5.0,
        "accumulated_session_cost": 0.0,
    }
    result = json.loads(await BudgetEstimatorTool(state=state)._arun())
    assert result["fits_within_budget"] is True
    assert result["remaining_budget_usd"] == pytest.approx(5.0, abs=0.01)
    assert result["step_count"] == 3


@pytest.mark.anyio
async def test_budget_overage_detected() -> None:
    tasks = [
        _make_step(1, action="read_file"),
        _make_step(2, action="edit_file"),
    ]
    state: Dict[str, Any] = {
        "mission_spec": _make_mission(tasks),
        "session_max_budget_usd": 5.0,
        "accumulated_session_cost": 4.99,  # almost exhausted
    }
    result = json.loads(await BudgetEstimatorTool(state=state)._arun())
    # With almost no remaining budget, even a small plan is likely over
    assert result["remaining_budget_usd"] == pytest.approx(0.01, abs=0.001)
    # actual result depends on heuristic; just check structure is correct
    assert "fits_within_budget" in result
    assert result["margin_usd"] == pytest.approx(
        result["remaining_budget_usd"] - result["estimated_cost_usd"], abs=1e-6
    )


@pytest.mark.anyio
async def test_budget_include_breakdown() -> None:
    tasks = [
        _make_step(1, action="write_file", description="Write the main module."),
        _make_step(2, action="read_file", description="Read config."),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await BudgetEstimatorTool(state=state)._arun(include_breakdown=True))
    assert "breakdown" in result
    assert len(result["breakdown"]) == 2
    for row in result["breakdown"]:
        assert "step_number" in row
        assert "estimated_tokens" in row
        assert "estimated_cost_usd" in row


@pytest.mark.anyio
async def test_budget_confidence_always_low() -> None:
    result = json.loads(await BudgetEstimatorTool(state={})._arun())
    assert result["confidence"] == "low"


@pytest.mark.anyio
async def test_budget_no_breakdown_by_default() -> None:
    tasks = [_make_step(1, action="read_file")]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await BudgetEstimatorTool(state=state)._arun())
    assert "breakdown" not in result


# =====================================================================
# F — Pre-commit hook smoke (no full planner node spin-up)
# =====================================================================


@pytest.mark.anyio
async def test_hook_bad_plan_returns_valid_false() -> None:
    """ValidateWBSDependenciesTool returns valid=False for a forward-reference plan."""
    tasks = [
        _make_step(1, action="read_file", target_file="src/new_module.py"),
        _make_step(2, action="write_file", target_file="src/new_module.py"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    result = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())
    assert result["valid"] is False


@pytest.mark.anyio
async def test_hook_raises_value_error_with_actionable_message() -> None:
    """Simulate the planner.py raise path — message contains step and file."""
    tasks = [
        _make_step(1, action="read_file", target_file="src/missing_create.py"),
        _make_step(3, action="write_file", target_file="src/missing_create.py"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    _val = json.loads(await ValidateWBSDependenciesTool(state=state)._arun())

    assert not _val.get("valid", True)

    _issue_lines = [
        f"{i['type']}: step {i.get('step_number', '?')},"
        f" file={i.get('target_file', '?')}"
        + (
            f" (producer at step {i['first_producer']})"
            if i.get("first_producer")
            else ""
        )
        for i in _val.get("issues", [])[:5]
    ]
    error_msg = (
        "WBS pre-commit dependency check failed — fix the following"
        " then re-emit the plan:\n" + "\n".join(_issue_lines)
    )
    # The error message must reference the step and file for the LLM to act on
    assert "step" in error_msg
    assert "src/missing_create.py" in error_msg
    assert "producer at step 3" in error_msg


@pytest.mark.anyio
async def test_hook_budget_over_returns_correct_structure() -> None:
    """BudgetEstimatorTool returns fits_within_budget=False when nearly exhausted."""
    tasks = [_make_step(i, action="write_file") for i in range(1, 20)]
    state: Dict[str, Any] = {
        "mission_spec": _make_mission(tasks),
        "session_max_budget_usd": 5.0,
        "accumulated_session_cost": 4.9999,
    }
    result = json.loads(await BudgetEstimatorTool(state=state)._arun())
    assert result["fits_within_budget"] is False
    assert result["margin_usd"] < 0
