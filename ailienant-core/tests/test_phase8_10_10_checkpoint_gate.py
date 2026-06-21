"""Checkpoint gate for sub-phase 8.10.10 — WBS contract correctness.

Group 1 (DEBT-044): DAG cycle detection via WBSStep.depends_on +
  ValidateWBSDependenciesTool Pass 5.
Group 2 (DEBT-051): BackgroundTaskManager owner-role filtering for
  non-orchestrator callers.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep
from tools.execution_tools import BackgroundTaskManager


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_step(step_number: int, depends_on: Any = None) -> WBSStep:
    return WBSStep(
        step_number=step_number,
        action="read_file",
        target_file=f"src/file_{step_number}.py",
        description=f"Read file {step_number}.",
        depends_on=depends_on,
    )


def _make_mission(steps: list[WBSStep]) -> MissionSpecification:
    return MissionSpecification(
        outcome="test outcome",
        scope=["src/"],
        constraints=[],
        decisions=[],
        tasks=steps,
        checks=[],
    )


async def _validate(mission: MissionSpecification) -> dict[str, Any]:
    from tools.planner_tools import ValidateWBSDependenciesTool
    tool = ValidateWBSDependenciesTool(state={"mission_spec": mission})
    result = await tool._arun(include_details=True)
    return json.loads(result)


# ──────────────────────────────────────────────────────────────────────────────
# Group 1 — DAG cycle detection (DEBT-044)
# ──────────────────────────────────────────────────────────────────────────────

def test_wbsstep_accepts_depends_on_field() -> None:
    step = _make_step(1, depends_on=[])
    assert step.depends_on == []
    step2 = _make_step(2, depends_on=[1])
    assert step2.depends_on == [1]


def test_wbsstep_depends_on_defaults_to_none() -> None:
    step = _make_step(3)
    assert step.depends_on is None


def test_validate_wbs_accepts_linear_chain() -> None:
    steps = [
        _make_step(1),
        _make_step(2, depends_on=[1]),
        _make_step(3, depends_on=[2]),
    ]
    result = asyncio.run(_validate(_make_mission(steps)))
    assert result["valid"] is True
    cycle_issues = [i for i in result["issues"] if i["type"] == "dependency_cycle"]
    assert cycle_issues == []


def test_validate_wbs_rejects_direct_cycle() -> None:
    steps = [
        _make_step(1, depends_on=[2]),
        _make_step(2, depends_on=[1]),
    ]
    result = asyncio.run(_validate(_make_mission(steps)))
    assert result["valid"] is False
    cycle_issues = [i for i in result["issues"] if i["type"] == "dependency_cycle"]
    assert len(cycle_issues) == 1
    assert set(cycle_issues[0]["cycle_steps"]) == {1, 2}


def test_validate_wbs_rejects_three_node_cycle() -> None:
    steps = [
        _make_step(1, depends_on=[3]),
        _make_step(2, depends_on=[1]),
        _make_step(3, depends_on=[2]),
    ]
    result = asyncio.run(_validate(_make_mission(steps)))
    assert result["valid"] is False
    cycle_issues = [i for i in result["issues"] if i["type"] == "dependency_cycle"]
    assert cycle_issues


def test_validate_wbs_rejects_missing_dep_ref() -> None:
    steps = [
        _make_step(1, depends_on=[99]),
    ]
    result = asyncio.run(_validate(_make_mission(steps)))
    assert result["valid"] is False
    invalid_issues = [i for i in result["issues"] if i["type"] == "invalid_depends_on"]
    assert len(invalid_issues) == 1
    assert invalid_issues[0]["missing_dep"] == 99


def test_validate_wbs_no_depends_on_skips_cycle_pass() -> None:
    steps = [_make_step(1), _make_step(2)]
    result = asyncio.run(_validate(_make_mission(steps)))
    assert result["valid"] is True
    dag_issues = [i for i in result["issues"] if i["type"] in ("dependency_cycle", "invalid_depends_on")]
    assert dag_issues == []


# ──────────────────────────────────────────────────────────────────────────────
# Group 2 — Role-scoped task visibility (DEBT-051)
# ──────────────────────────────────────────────────────────────────────────────

def _make_manager() -> BackgroundTaskManager:
    registry: Dict[str, Any] = {}
    return BackgroundTaskManager(registry)


def _stub_proc(pid: int = 1234) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    return proc


async def _create_task(manager: BackgroundTaskManager, cmd: str, owner_role: str | None) -> str:
    with patch("tools.execution_tools.asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_shell:
        mock_shell.return_value = _stub_proc()
        with patch.object(manager, "_watch", new_callable=AsyncMock):
            return await manager.create(cmd, owner_role=owner_role)


def test_task_create_stamps_owner_role() -> None:
    manager = _make_manager()
    task_id = asyncio.run(_create_task(manager, "echo hi", "core_dev"))
    tasks = manager.list_tasks(caller_role="core_dev")
    assert task_id in tasks
    assert tasks[task_id]["owner_role"] == "core_dev"


def test_task_list_filters_non_orchestrator() -> None:
    manager = _make_manager()
    tid_coder = asyncio.run(_create_task(manager, "echo coder", "core_dev"))
    tid_orch = asyncio.run(_create_task(manager, "echo orch", "orchestrator"))
    coder_view = manager.list_tasks(caller_role="core_dev")
    assert tid_coder in coder_view
    assert tid_orch not in coder_view


def test_task_list_orchestrator_sees_all() -> None:
    manager = _make_manager()
    tid_coder = asyncio.run(_create_task(manager, "echo coder", "core_dev"))
    tid_orch = asyncio.run(_create_task(manager, "echo orch", "orchestrator"))
    orch_view = manager.list_tasks(caller_role="orchestrator")
    assert tid_coder in orch_view
    assert tid_orch in orch_view


def test_task_list_no_caller_role_sees_all() -> None:
    manager = _make_manager()
    tid_a = asyncio.run(_create_task(manager, "echo a", "core_dev"))
    tid_b = asyncio.run(_create_task(manager, "echo b", "qa_tester"))
    full_view = manager.list_tasks(caller_role=None)
    assert tid_a in full_view
    assert tid_b in full_view


def test_task_list_excludes_raw_output() -> None:
    manager = _make_manager()
    tid = asyncio.run(_create_task(manager, "echo hello", "core_dev"))
    view = manager.list_tasks()
    entry = view[tid]
    assert "truncated_stdout" not in entry
    assert "truncated_stderr" not in entry


def test_task_create_without_owner_role_stores_none() -> None:
    manager = _make_manager()
    tid = asyncio.run(_create_task(manager, "echo x", None))
    all_tasks = manager.list_tasks()
    assert all_tasks[tid]["owner_role"] is None
