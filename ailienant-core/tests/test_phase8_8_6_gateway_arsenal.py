"""Gateway Arsenal gate — sibling-file checkpoint.

DoD (RBAC parity + substrate contract):
  - 4 gateway schemas register; each tool is gated to its owning role(s).
  - task_create and task_get (Task V2) carry orchestrator in allowed_roles.
  - Tiers: list_capabilities/skill_invoke/task_list are READ_ONLY and survive
    PLAN mode; task_stop is EXECUTE and is dropped in PLAN.
  - Negative RBAC: doc_manager cannot reach list_capabilities; a role that cannot
    create a background task cannot list or stop one either.
  - BackgroundTaskManager hardening: cancel wins the race over _watch; _procs pop
    is in a finally block; dead-process terminate is safe.

The benchmark pair this file also covered was removed with the tools themselves —
gateway/handlers.py is their canonical owner. Coverage of deleted code has nothing
to backfill.
"""
from __future__ import annotations

import hashlib
import json
import struct
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
import tools.execution_tools as execution_tools
from tools.execution_tools import (
    BackgroundTaskManager,
    _EXECUTE_ROLES,
    TASK_CREATE_ROLES,
    _TASK_GET_ROLES,
    register_execution_tools,
)
from tools.gateway_tools import (
    _CATALOG_ROLES,
    _SKILL_ROLES,
    _TASK_MGR_ROLES,
    ListCapabilitiesTool,
    SkillInvokeTool,
    TaskListTool,
    TaskStopTool,
    register_gateway_tools,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_task_service() -> Any:
    """Reset the process-wide TaskService around every test (R2).

    The singleton retains its active-task registry for the life of the process;
    without this, a benchmark registration in one case would leak into the next
    and produce order-dependent flakes.
    """
    from core.task_service import reset_task_service

    reset_task_service()
    yield
    reset_task_service()


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
        store_path=str(tmp_path / "tool_rag_886"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


async def _register_all(store: ToolRAGStore) -> None:
    await register_gateway_tools(store)
    await register_execution_tools(store)


def _by_name(store: ToolRAGStore) -> Dict[str, ToolSchema]:
    return {s.name: s for s in store.all_schemas()}


def _make_manager() -> BackgroundTaskManager:
    return BackgroundTaskManager(registry={})


# =====================================================================
# A — Registration + role sets
# =====================================================================


@pytest.mark.anyio
async def test_register_gateway_tools_returns_four(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_gateway_tools(store)
    assert count == 4


@pytest.mark.anyio
async def test_all_four_names_present(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_gateway_tools(store)
    names = set(_by_name(store))
    assert names == {
        "list_capabilities",
        "skill_invoke",
        "task_list",
        "task_stop",
    }


@pytest.mark.anyio
async def test_role_sets_match_constants(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_gateway_tools(store)
    roles = {n: s.allowed_roles for n, s in _by_name(store).items()}
    assert roles["list_capabilities"] == _CATALOG_ROLES
    assert roles["skill_invoke"] == _SKILL_ROLES
    assert roles["task_list"] == _TASK_MGR_ROLES
    assert roles["task_stop"] == _TASK_MGR_ROLES


@pytest.mark.anyio
async def test_task_create_v2_extended_to_orchestrator(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    schemas = _by_name(store)
    assert "orchestrator" in schemas["task_create"].allowed_roles
    assert _EXECUTE_ROLES.issubset(schemas["task_create"].allowed_roles)
    assert schemas["task_create"].allowed_roles == TASK_CREATE_ROLES


@pytest.mark.anyio
async def test_task_get_v2_extended_to_orchestrator(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    schemas = _by_name(store)
    assert "orchestrator" in schemas["task_get"].allowed_roles
    assert schemas["task_get"].allowed_roles == _TASK_GET_ROLES


# =====================================================================
# B — Tier assignments + PLAN mode survival
# =====================================================================


@pytest.mark.anyio
async def test_tier_assignments(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_gateway_tools(store)
    tiers = {n: s.privilege_tier for n, s in _by_name(store).items()}
    assert tiers["list_capabilities"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["skill_invoke"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["task_list"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["task_stop"] == ToolPrivilegeTier.EXECUTE


@pytest.mark.anyio
async def test_read_only_tools_survive_plan_mode(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    for tool_name in ("list_capabilities", "skill_invoke", "task_list"):
        results = await store.select_tools(
            tool_name, k=10,
            active_role="orchestrator",
            session_mode=SessionPermissionMode.PLAN,
        )
        assert any(s.name == tool_name for s in results), (
            f"{tool_name!r} should survive PLAN mode"
        )


@pytest.mark.anyio
async def test_execute_tools_dropped_in_plan_mode(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    for tool_name in ("task_stop",):
        results = await store.select_tools(
            tool_name, k=10,
            active_role="orchestrator",
            session_mode=SessionPermissionMode.PLAN,
        )
        assert not any(s.name == tool_name for s in results), (
            f"{tool_name!r} should be dropped in PLAN mode"
        )


# =====================================================================
# C — Negative RBAC (the DoD)
# =====================================================================


@pytest.mark.anyio
async def test_doc_manager_cannot_list_capabilities(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    schemas = _by_name(store)
    assert "doc_manager" not in schemas["list_capabilities"].allowed_roles


@pytest.mark.anyio
async def test_vcs_manager_cannot_stop_tasks(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    schemas = _by_name(store)
    assert "vcs_manager" not in schemas["task_stop"].allowed_roles


@pytest.mark.anyio
async def test_task_management_audience_equals_task_creator_audience(tmp_path: Path) -> None:
    """Whoever can spawn a background task can also list and stop one.

    Asserted as an equality rather than by naming a role: the asymmetry this
    replaces (create without stop) left a hung task with no cleanup path, and a
    hardcoded probe role is what let the two halves drift apart in the first place.
    """
    store = _isolated_store(tmp_path)
    await _register_all(store)
    schemas = _by_name(store)
    creators = schemas["task_create"].allowed_roles
    for managed in ("task_list", "task_stop"):
        assert creators <= schemas[managed].allowed_roles, (
            f"a role may create a task but not {managed}"
        )
    # Still a real gate: a non-creator role reaches neither.
    assert "doc_manager" not in schemas["task_list"].allowed_roles
    assert "doc_manager" not in schemas["task_stop"].allowed_roles


# =====================================================================
# D — Behaviour smoke tests
# =====================================================================


@pytest.mark.anyio
async def test_list_capabilities_returns_catalog_names(tmp_path: Path) -> None:
    tool = ListCapabilitiesTool()
    result = await tool._arun()
    caps = json.loads(result)
    names = {c["name"] for c in caps}
    # At minimum the core 8.5 capabilities must be present
    assert "run_task" in names
    assert "run_benchmark" in names
    assert "get_report" in names


@pytest.mark.anyio
async def test_skill_invoke_returns_shaped_json(tmp_path: Path) -> None:
    tool = SkillInvokeTool()
    fake_skills = [{"id": "s1", "name": "refactor", "body": "Prefer small PRs."}]
    with patch("core.skill_resolver.resolve_active_skills", new_callable=AsyncMock) as mock_rs:
        mock_rs.return_value = fake_skills
        result = await tool._arun(
            user_input="refactor this module",
            workspace_root="/workspace",
        )
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["skills"] == fake_skills


@pytest.mark.anyio
async def test_task_list_empty_registry(tmp_path: Path) -> None:
    tool = TaskListTool(manager=_make_manager())
    result = await tool._arun()
    payload = json.loads(result)
    assert payload["count"] == 0
    assert payload["tasks"] == {}
    assert payload["truncated"] is False


@pytest.mark.anyio
async def test_task_stop_not_found(tmp_path: Path) -> None:
    tool = TaskStopTool(manager=_make_manager())
    result = await tool._arun(task_id="nonexistent")
    payload = json.loads(result)
    assert payload["status"] == "not_found_or_completed"


@pytest.mark.anyio
async def test_task_stop_happy_path(tmp_path: Path) -> None:
    manager = _make_manager()
    fake_proc = MagicMock()
    fake_proc.terminate = MagicMock()
    task_id = uuid.uuid4().hex
    manager._registry[task_id] = {"status": "running", "pid": 1234}
    manager._procs[task_id] = fake_proc

    tool = TaskStopTool(manager=manager)
    result = await tool._arun(task_id=task_id)
    payload = json.loads(result)
    assert payload["status"] == "cancelled"
    assert payload["task_id"] == task_id
    fake_proc.terminate.assert_called_once()
    assert manager._registry[task_id]["status"] == "cancelled"
    assert task_id not in manager._procs


@pytest.mark.anyio
async def test_skill_invoke_rejects_empty_workspace_root(tmp_path: Path) -> None:
    tool = SkillInvokeTool()
    result = await tool._arun(user_input="refactor", workspace_root="  ")
    assert result.startswith("[skill_invoke] REJECTED")


@pytest.mark.anyio
async def test_background_task_manager_list_excludes_output_keys(tmp_path: Path) -> None:
    manager = _make_manager()
    task_id = uuid.uuid4().hex
    manager._registry[task_id] = {
        "command": "pytest",
        "status": "running",
        "truncated_stdout": "a" * 200,
        "truncated_stderr": "e" * 200,
    }
    snapshot = manager.list_tasks()
    assert task_id in snapshot
    entry = snapshot[task_id]
    assert "truncated_stdout" not in entry
    assert "truncated_stderr" not in entry
    assert entry["command"] == "pytest"
    assert entry["status"] == "running"


@pytest.mark.anyio
async def test_background_task_manager_stop_sets_cancelled_before_terminate(
    tmp_path: Path,
) -> None:
    manager = _make_manager()
    task_id = uuid.uuid4().hex
    order: List[str] = []
    manager._registry[task_id] = {"status": "running"}

    class _OrderedProc:
        returncode: Optional[int] = None
        # On Windows, stop() force-kills the tree unconditionally (the shell's
        # own clean exit doesn't prove a grandchild didn't survive it — see
        # BackgroundTaskManager._force_kill's docstring), so this fake needs a
        # real-shaped .pid for that taskkill call to construct, exactly like
        # _TrappingProc below.
        pid = 4321

        def terminate(self) -> None:
            order.append("terminate")
            # status must already be "cancelled" at this point
            assert manager._registry[task_id]["status"] == "cancelled", (
                "status must be committed to 'cancelled' before terminate() is called"
            )
            self.returncode = 0  # exits cleanly within the grace window

    manager._procs[task_id] = _OrderedProc()  # type: ignore[assignment]
    result = await manager.stop(task_id)
    assert result is True
    assert "terminate" in order
    assert manager._registry[task_id]["status"] == "cancelled"
    assert task_id not in manager._procs


@pytest.mark.anyio
async def test_background_task_manager_stop_survives_dead_process(tmp_path: Path) -> None:
    manager = _make_manager()
    task_id = uuid.uuid4().hex
    manager._registry[task_id] = {"status": "running"}
    fake_proc = MagicMock()
    fake_proc.terminate.side_effect = ProcessLookupError("already gone")
    manager._procs[task_id] = fake_proc

    result = await manager.stop(task_id)
    assert result is True
    assert manager._registry[task_id]["status"] == "cancelled"
    assert task_id not in manager._procs  # guaranteed by finally block


@pytest.mark.anyio
async def test_background_task_manager_stop_escalates_to_force_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that ignores the soft signal is force-killed after the grace window."""
    monkeypatch.setattr(execution_tools, "_STOP_GRACE_S", 0.05)
    monkeypatch.setattr(execution_tools, "_STOP_POLL_INTERVAL_S", 0.01)
    manager = _make_manager()
    task_id = uuid.uuid4().hex
    manager._registry[task_id] = {"status": "running"}

    class _TrappingProc:
        returncode: Optional[int] = None  # never exits on its own
        pid = 4321

        def terminate(self) -> None:
            pass  # traps the soft signal

    proc = _TrappingProc()
    manager._procs[task_id] = proc  # type: ignore[assignment]

    killed: List[Any] = []

    async def _fake_force_kill(p: Any) -> None:
        killed.append(p)

    monkeypatch.setattr(
        BackgroundTaskManager, "_force_kill", staticmethod(_fake_force_kill)
    )

    result = await manager.stop(task_id)
    assert result is True
    assert killed == [proc]  # escalated after the grace window elapsed
    assert task_id not in manager._procs


@pytest.mark.anyio
async def test_background_task_manager_watch_respects_cancel_race(tmp_path: Path) -> None:
    """_watch must not overwrite 'cancelled' with 'completed' when stop() wins the race."""
    manager = _make_manager()
    task_id = uuid.uuid4().hex
    manager._registry[task_id] = {"status": "cancelled"}  # stop() already committed

    # Simulate _watch waking up after proc.communicate() — returncode 0 would mean
    # "completed", but status is already "cancelled" so _watch must return early.
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"out", b""))
    proc.returncode = 0

    await manager._watch(task_id, proc)

    # Status must NOT have been overwritten to "completed"
    assert manager._registry[task_id]["status"] == "cancelled"


# =====================================================================
# F — Task V2 extend: orchestrator surfaces task_create + task_get
# =====================================================================


@pytest.mark.anyio
async def test_orchestrator_surfaces_task_create(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    results = await store.select_tools(
        "spawn background subprocess task",
        k=10,
        active_role="orchestrator",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert any(s.name == "task_create" for s in results)


@pytest.mark.anyio
async def test_orchestrator_surfaces_task_get(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    results = await store.select_tools(
        "get task status output",
        k=10,
        active_role="orchestrator",
        session_mode=SessionPermissionMode.PLAN,
    )
    assert any(s.name == "task_get" for s in results)
