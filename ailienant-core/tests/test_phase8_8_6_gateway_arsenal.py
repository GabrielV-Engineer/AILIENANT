"""Wave 5 Gateway/Benchmark Arsenal gate — sibling-file checkpoint.

DoD (RBAC parity + substrate contract):
  - 6 net-new gateway schemas register; each tool is gated to its owning role(s).
  - task_create and task_get (Task V2) carry orchestrator in allowed_roles.
  - Tiers: get_benchmark_report/list_capabilities/skill_invoke/task_list are
    READ_ONLY and survive PLAN mode; run_benchmark/task_stop are EXECUTE and are
    dropped in PLAN.
  - Negative RBAC: core_dev cannot reach benchmark tools; doc_manager cannot
    reach list_capabilities; vcs_manager cannot reach task_stop.
  - Benchmark tools call the same substrate functions as the 8.5 verbs (no
    duplicated runner). BackgroundTaskManager hardening: cancel wins the race
    over _watch; _procs pop is in a finally block; dead-process terminate is safe.
"""
from __future__ import annotations

import asyncio
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
    _TASK_CREATE_ROLES,
    _TASK_GET_ROLES,
    register_execution_tools,
)
from tools.gateway_tools import (
    _BENCHMARK_ROLES,
    _CATALOG_ROLES,
    _SKILL_ROLES,
    _TASK_MGR_ROLES,
    _cleanup_benchmark,
    GetBenchmarkReportTool,
    ListCapabilitiesTool,
    RunBenchmarkTool,
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
async def test_register_gateway_tools_returns_six(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_gateway_tools(store)
    assert count == 6


@pytest.mark.anyio
async def test_all_six_names_present(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_gateway_tools(store)
    names = set(_by_name(store))
    assert names == {
        "run_benchmark",
        "get_benchmark_report",
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
    assert roles["run_benchmark"] == _BENCHMARK_ROLES
    assert roles["get_benchmark_report"] == _BENCHMARK_ROLES
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
    assert schemas["task_create"].allowed_roles == _TASK_CREATE_ROLES


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
    assert tiers["run_benchmark"] == ToolPrivilegeTier.EXECUTE
    assert tiers["get_benchmark_report"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["list_capabilities"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["skill_invoke"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["task_list"] == ToolPrivilegeTier.READ_ONLY
    assert tiers["task_stop"] == ToolPrivilegeTier.EXECUTE


@pytest.mark.anyio
async def test_read_only_tools_survive_plan_mode(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    for tool_name in ("get_benchmark_report", "list_capabilities", "skill_invoke", "task_list"):
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
    for tool_name in ("run_benchmark", "task_stop"):
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
async def test_core_dev_cannot_access_benchmark_tools(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    schemas = _by_name(store)
    assert "core_dev" not in schemas["run_benchmark"].allowed_roles
    assert "core_dev" not in schemas["get_benchmark_report"].allowed_roles


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
async def test_qa_tester_cannot_list_tasks(tmp_path: Path) -> None:
    """task_list is orchestrator-only; qa_tester holds run_benchmark but not task_list."""
    store = _isolated_store(tmp_path)
    await _register_all(store)
    schemas = _by_name(store)
    assert "qa_tester" not in schemas["task_list"].allowed_roles


# =====================================================================
# D — Behaviour smoke tests
# =====================================================================


@pytest.mark.anyio
async def test_get_benchmark_report_pending(tmp_path: Path) -> None:
    tool = GetBenchmarkReportTool()
    fake_result = {"status": "running", "task_id": "abc"}
    with patch("core.benchmark_service.read_report", return_value=fake_result) as mock_rr:
        result = await tool._arun(task_id="a" * 32)
    assert json.loads(result)["status"] == "running"
    mock_rr.assert_called_once_with("a" * 32)


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


# =====================================================================
# E — DoD substrate contract + audit hardening
# =====================================================================


@pytest.mark.anyio
async def test_run_benchmark_busy_returns_busy_dict(tmp_path: Path) -> None:
    tool = RunBenchmarkTool()
    with patch("core.benchmark_service.try_reserve", return_value=False):
        result = await tool._arun(suite="v1")
    assert json.loads(result)["status"] == "busy"


@pytest.mark.anyio
async def test_run_benchmark_unknown_suite_rejected(tmp_path: Path) -> None:
    tool = RunBenchmarkTool()
    with patch("core.benchmark_service.try_reserve", side_effect=ValueError("unknown suite: 'bad'")):
        result = await tool._arun(suite="bad")
    assert result.startswith("[run_benchmark] REJECTED")


@pytest.mark.anyio
async def test_run_benchmark_happy_path_charges_registers_and_wires_callback(
    tmp_path: Path,
) -> None:
    """DEBT-050: upfront ledger charge · DEBT-048: register_active_task · callback wired."""
    tool = RunBenchmarkTool()
    captured_callbacks: List[Any] = []

    class _FakeTask:
        def add_done_callback(self, cb: Any) -> None:
            captured_callbacks.append(cb)

        def cancel(self) -> None:  # pragma: no cover — not reached on the happy path
            pass

    async def _noop_run(task_id: str, suite: str = "v1") -> None:
        return None

    charged: List[Any] = []

    async def _fake_consume(caller_id: str, amount: float) -> None:
        charged.append((caller_id, amount))

    registered: List[Any] = []
    fake_ts = MagicMock()
    fake_ts.register_active_task.side_effect = lambda tid, t: registered.append((tid, t))

    with (
        patch("core.benchmark_service.try_reserve", return_value=True),
        patch("core.benchmark_service.run_benchmark", side_effect=_noop_run),
        patch("asyncio.create_task", return_value=_FakeTask()),
        patch("gateway.ledger.consume_budget", side_effect=_fake_consume),
        patch("core.task_service.get_task_service", return_value=fake_ts),
    ):
        result = await tool._arun(suite="v1")

    payload = json.loads(result)
    assert payload["status"] == "submitted"
    assert "task_id" in payload
    assert payload["poll"] == "check_task_status"  # task_get reads a different registry
    assert len(captured_callbacks) == 1  # only _cleanup_benchmark (registration is mocked)
    assert charged and charged[0][0] == "internal:agent"  # DEBT-050 upfront charge
    assert registered and registered[0][0] == payload["task_id"]  # DEBT-048 registration


@pytest.mark.anyio
async def test_run_benchmark_refunds_and_releases_on_spawn_failure(tmp_path: Path) -> None:
    """A spawn failure after the upfront charge must refund and release the slot."""
    tool = RunBenchmarkTool()
    consume_calls: List[Any] = []

    async def _fake_consume(caller_id: str, amount: float) -> None:
        consume_calls.append((caller_id, amount))

    with (
        patch("core.benchmark_service.try_reserve", return_value=True),
        patch("core.benchmark_service.release_flight") as mock_release,
        patch("asyncio.create_task", side_effect=RuntimeError("loop gone")),
        patch("gateway.ledger.consume_budget", side_effect=_fake_consume),
    ):
        with pytest.raises(RuntimeError):
            await tool._arun(suite="v1")

    # Charged once (+cost), then refunded once (-cost); slot released.
    assert len(consume_calls) == 2
    assert consume_calls[0][1] == -consume_calls[1][1]
    mock_release.assert_called_once()


@pytest.mark.anyio
async def test_cleanup_benchmark_logs_exception(tmp_path: Path, caplog: Any) -> None:
    """_cleanup_benchmark must log failures with exc_info, not raise (§5.2 / §12)."""
    import logging

    err = RuntimeError("harness exploded")
    task = MagicMock(spec=asyncio.Task)
    task.cancelled.return_value = False
    task.exception.return_value = err

    with (
        patch("core.benchmark_service.release_flight"),
        caplog.at_level(logging.ERROR, logger="GATEWAY_TOOLS"),
    ):
        _cleanup_benchmark(task, suite="v1")

    assert any("v1" in r.message or "harness" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_cleanup_benchmark_does_not_raise_on_cancelled(tmp_path: Path) -> None:
    """A cancelled task must not trigger task.exception() (which raises CancelledError)."""
    task = MagicMock(spec=asyncio.Task)
    task.cancelled.return_value = True

    with patch("core.benchmark_service.release_flight"):
        _cleanup_benchmark(task, suite="v1")  # must not raise

    task.exception.assert_not_called()


@pytest.mark.anyio
async def test_get_benchmark_report_uses_asyncio_to_thread(tmp_path: Path) -> None:
    """read_report is sync disk I/O; it must run in a thread, not on the event loop."""
    tool = GetBenchmarkReportTool()
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_tt:
        mock_tt.return_value = {"status": "running", "task_id": "x" * 32}
        await tool._arun(task_id="x" * 32)
    mock_tt.assert_called_once()


@pytest.mark.anyio
async def test_get_benchmark_report_rejects_malformed_task_id(tmp_path: Path) -> None:
    tool = GetBenchmarkReportTool()
    # Patch asyncio.to_thread to raise ValueError (as _resolve_artifact would)
    with patch("asyncio.to_thread", side_effect=ValueError("invalid task_id: 'bad!'")):
        result = await tool._arun(task_id="bad!")
    payload = json.loads(result)
    assert payload["status"] == "rejected"
    assert "invalid" in payload["detail"]


@pytest.mark.anyio
async def test_get_benchmark_report_handles_file_not_found(tmp_path: Path) -> None:
    """TOCTOU: file was present at exists() but vanished before read_text() in thread."""
    tool = GetBenchmarkReportTool()
    with patch("asyncio.to_thread", new_callable=AsyncMock, side_effect=FileNotFoundError("gone")):
        result = await tool._arun(task_id="a" * 32)
    payload = json.loads(result)
    assert payload["status"] == "rejected"


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

        def terminate(self) -> None:
            order.append("terminate")
            # status must already be "cancelled" at this point
            assert manager._registry[task_id]["status"] == "cancelled", (
                "status must be committed to 'cancelled' before terminate() is called"
            )
            self.returncode = 0  # exits cleanly within the grace window — no escalation

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
