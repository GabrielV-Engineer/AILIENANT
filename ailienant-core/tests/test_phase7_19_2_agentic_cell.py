"""Phase 7.19.2 DoD — Agentic Execution Cell (ReAct sub-loop).

Directed suite for the autonomous run-read-edit-rerun loop: the cell drives a stub
session to green in the same turn, leaves a trajectory record per iteration, keeps the
trivial run_command path intact, audits tool-args, returns structured diagnostics (never
raw stdout), bypasses the response cache, governs competing fix candidates with a
transactional contained MCTS (rolling the surface back between candidates), bounds a
non-converging loop, and never strands a session.

Async cases run inside a single ``asyncio.run`` so the persistent stub session and its
background collector live on one event loop across loop-back iterations.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import brain.agentic_cell as ac
from brain.agentic_cell import (
    ToolCall,
    _read_file_ast,
    audit_tool_args,
    route_after_cell,
    run_agentic_cell_node,
    select_candidate_via_mcts,
)
from brain.retry_policy import AGENTIC_CELL_MAX_ITERATIONS
from brain.state import MissionSpecification, WBSStep
from core.blob_storage import ContentAddressableStorage
from core.workspace_sync import SyncSurface, _raw_sha256


# ── Stubs ───────────────────────────────────────────────────────────────────────


class StubSyncSurface(SyncSurface):
    """In-memory work surface that records every write (for rollback assertions)."""

    def __init__(self, initial: Optional[Dict[str, bytes]] = None) -> None:
        self._files: Dict[str, bytes] = dict(initial or {})
        self.write_log: List[Tuple[str, bytes]] = []

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content
        self.write_log.append((rel_path, content))

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}

    def writes_for(self, path: str) -> List[bytes]:
        return [c for p, c in self.write_log if p == path]


class StubSession:
    """A scripted persistent session: each run() yields the next output + exit code."""

    def __init__(self, exit_codes: List[int], outputs: List[bytes]) -> None:
        self._exit_codes = list(exit_codes)
        self._outputs = list(outputs)
        self._q: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._closed = False
        self.run_calls: List[str] = []
        self.closed = False
        self.killed = False

    async def start(self) -> None:
        return None

    async def run(self, command: str, *, timeout_s: float) -> int:
        self.run_calls.append(command)
        out = self._outputs.pop(0) if len(self._outputs) > 1 else (self._outputs[0] if self._outputs else b"")
        await self._q.put(out)
        return self._exit_codes.pop(0) if len(self._exit_codes) > 1 else (self._exit_codes[0] if self._exit_codes else 0)

    async def stream(self) -> Any:
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(self._q.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            yield chunk

    async def write_stdin(self, data: bytes) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def kill(self) -> None:
        self.killed = True
        self.closed = True
        self._closed = True

    async def close(self) -> None:
        self.closed = True
        self._closed = True


class StubAdapter:
    """Session-capable adapter handing out one stub session + surface per task."""

    supports_sessions = True

    def __init__(self, session: StubSession, surface: StubSyncSurface) -> None:
        self._session = session
        self._surface = surface

    async def open_session(self, *, cwd: str, env_whitelist: Dict[str, str], **_: Any) -> StubSession:
        return self._session

    def get_sync_surface(self, cwd: str) -> StubSyncSurface:
        return self._surface


def _reasoner_from(scripts: List[List[ToolCall]]) -> ac.CellReasoner:
    """Return a reasoner that emits one scripted tool-call list per call, in order."""
    calls = {"i": 0}

    async def _reason(_messages: Any) -> List[ToolCall]:
        idx = min(calls["i"], len(scripts) - 1)
        calls["i"] += 1
        return scripts[idx]

    return _reason


def _config(adapter: StubAdapter, reasoner: ac.CellReasoner, **extra: Any) -> Dict[str, Any]:
    configurable: Dict[str, Any] = {"cell_adapter": adapter, "cell_reasoner": reasoner}
    configurable.update(extra)
    return {"configurable": configurable}


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "cell-test",
        "user_input": "make the tests pass",
        "workspace_root": "/work",
        "vfs_buffer": {},
        "agentic_iteration": 0,
        "agentic_trajectory": [],
        "session_permission_mode": "AUTO",
        "mission_spec": None,
    }
    state.update(overrides)
    return state


def _apply(state: Dict[str, Any], delta: Dict[str, Any]) -> None:
    """Apply a node delta to the local state the way the graph reducers would."""
    for key, value in delta.items():
        if key in ("agentic_trajectory", "permission_audit_log", "security_flags", "errors"):
            state[key] = (state.get(key) or []) + value
        elif key in ("vfs_buffer", "pending_contents"):
            merged = dict(state.get(key) or {})
            merged.update(value)
            state[key] = merged
        else:
            state[key] = value


async def _drive(state: Dict[str, Any], config: Dict[str, Any], max_visits: int = 30) -> int:
    """Run the cell loop-back to completion inside one event loop; return visit count."""
    visits = 0
    while True:
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)
        visits += 1
        if route_after_cell(state) != "agentic_cell" or visits >= max_visits:
            break
    return visits


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# ── DoD rows ──────────────────────────────────────────────────────────────────


def test_cell_runs_until_green() -> None:
    """exit != 0 then exit 0 → the cell loops twice and exits green, same turn."""
    session = StubSession(exit_codes=[1, 0], outputs=[b"E   assert 1 == 2\n", b"1 passed\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "python -m pytest -q"})]])
    state = _base_state()

    visits = asyncio.run(_drive(state, _config(adapter, reasoner)))

    assert visits == 2
    assert state["agentic_trajectory"][-1]["status"] == "green"
    assert session.run_calls == ["python -m pytest -q", "python -m pytest -q"]


def test_each_iteration_records_trajectory() -> None:
    session = StubSession(exit_codes=[1, 0], outputs=[b"fail\n", b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner)))

    iteration_records = [r for r in state["agentic_trajectory"] if "iteration" in r]
    assert [r["iteration"] for r in iteration_records] == [0, 1]


def test_trivial_run_command_routes_to_coder() -> None:
    from brain.engine import _coder_target

    step = WBSStep(step_number=1, action="run_command", target_file="ls", description="list", requires_iteration=False)
    assert _coder_target(step) == "coder_agent"


def test_flagged_step_routes_to_cell() -> None:
    from brain.engine import _coder_target

    step = WBSStep(step_number=1, action="run_command", target_file="pytest", description="fix tests", requires_iteration=True)
    assert _coder_target(step) == "agentic_cell"


def test_run_terminal_structured_diagnostics() -> None:
    """The verdict is a structured diagnostic summary, not the raw stdout."""
    raw = b"FAILED tests/test_x.py::test_y - AssertionError: boom\nlots of other raw noise\n"
    session = StubSession(exit_codes=[0], outputs=[raw])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "python -m pytest -q"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner)))

    diagnostics = state["agentic_trajectory"][0]["diagnostics"]
    assert diagnostics != raw.decode("utf-8")
    assert "lots of other raw noise" not in diagnostics


def test_apply_granular_edit_occ() -> None:
    """apply_granular_edit dispatches through apply_patch_to_vfs with an OCC expected_hash."""
    captured: Dict[str, Any] = {}

    def fake_apply(read: Any, write: Any, path: str, search: str, replace: str, expected_hash: Optional[str] = None) -> str:
        captured["expected_hash"] = expected_hash
        write(path, replace)
        return "diff"

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("apply_granular_edit", {"path": "a.py", "search": "x = 1", "replace": "x = 2"})]])
    state = _base_state()

    with patch("tools.patch_tool.apply_patch_to_vfs", side_effect=fake_apply):
        asyncio.run(_drive(state, _config(adapter, reasoner)))

    assert captured.get("expected_hash") is not None
    assert state["pending_contents"]["a.py"] == "x = 2"


def test_occ_conflict_injects_system_diagnostic() -> None:
    """A stale edit injects a system diagnostic so the model re-reads (no livelock)."""
    from core.exceptions import StaleFileException

    def raise_stale(*_a: Any, **_k: Any) -> str:
        raise StaleFileException("file drifted")

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("apply_granular_edit", {"path": "a.py", "search": "x = 1", "replace": "x = 2"})]])
    state = _base_state()

    with patch("tools.patch_tool.apply_patch_to_vfs", side_effect=raise_stale):
        asyncio.run(_drive(state, _config(adapter, reasoner)))

    system_records = [r for r in state["agentic_trajectory"] if r.get("role") == "system"]
    assert any("OCC conflict" in r.get("content", "") for r in system_records)


def test_read_file_ast_skeleton() -> None:
    """read_file_ast returns an AST skeleton (body elided), not the full source."""
    source = "def add(a, b):\n    secret_token = 'xyz'\n    return a + b\n"
    skeleton = _read_file_ast(source, "calc.py")
    assert "def add" in skeleton
    assert "secret_token" not in skeleton


def test_cache_bypassed() -> None:
    """The cell never probes the semantic response cache."""
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    with patch("core.response_cache.response_cache.probe", new=MagicMock()) as probe:
        asyncio.run(_drive(state, _config(adapter, reasoner)))
        probe.assert_not_called()


def test_tool_args_injection_audited() -> None:
    """A DANGEROUS run_terminal arg is rejected, recorded scrubbed, and never executed."""
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "rm -rf /"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner)))

    assert "SECURITY_TOOL_ARG_REJECTED" in (state.get("security_flags") or [])
    assert session.run_calls == []  # the dangerous command never ran
    entries = state.get("permission_audit_log") or []
    assert any(e["tool"] == "run_terminal" and e["decision"] == "reject" for e in entries)


def test_audit_scrubs_secrets() -> None:
    outcome = audit_tool_args("run_terminal", {"command": "echo sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"})
    assert "sk-ant-" not in outcome.entry["args"]["command"]


def test_mcts_branch_selects_best_verdict() -> None:
    """>=2 candidates → UCB1 over the structured verdict picks the passing one."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    candidates = [{"a.py": "BROKEN\n"}, {"a.py": "good\n"}]

    async def run_verify(_cmd: str) -> Tuple[int, str]:
        content = (await surface.read_file("a.py") or b"").decode("utf-8")
        return (0, "") if "good" in content else (1, "FAILED")

    async def body() -> Tuple[int, Dict[str, str]]:
        return await select_candidate_via_mcts(
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            candidates=candidates,
            verify_command="pytest",
            run_verify=run_verify,
            blob_store=store,
            mission_state=None,
        )

    winner_index, winner = asyncio.run(body())
    assert winner_index == 1
    assert winner["a.py"] == "good\n"


def test_mcts_rolls_back_surface_between_candidates() -> None:
    """The surface is rolled back to the clean base between candidates and ends at the winner."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    base = "BASE\n"
    candidates = [{"a.py": "BROKEN\n"}, {"a.py": "good\n"}]

    async def run_verify(_cmd: str) -> Tuple[int, str]:
        content = (await surface.read_file("a.py") or b"").decode("utf-8")
        return (0, "") if "good" in content else (1, "FAILED")

    async def body() -> None:
        await select_candidate_via_mcts(
            surface=surface,
            clean_base_content={"a.py": base},
            candidates=candidates,
            verify_command="pytest",
            run_verify=run_verify,
            blob_store=store,
            mission_state=None,
        )

    asyncio.run(body())

    writes = [c.decode("utf-8") for c in surface.writes_for("a.py")]
    # cand0 → rollback(base) → cand1 → rollback(base) → winner
    assert writes == ["BROKEN\n", base, "good\n", base, "good\n"]
    final = surface._files["a.py"].decode("utf-8")
    assert final == "good\n"


def test_budget_bound_concedes() -> None:
    """A never-converging loop stops at the iteration ceiling with an honest concede."""
    session = StubSession(exit_codes=[1], outputs=[b"still failing\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    visits = asyncio.run(_drive(state, _config(adapter, reasoner), max_visits=50))

    assert visits == AGENTIC_CELL_MAX_ITERATIONS
    assert state["agentic_trajectory"][-1]["status"] == "budget"


def test_session_closed_on_terminal_exit() -> None:
    """The persistent session is closed (and deregistered) on terminal exit — no leak."""
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner)))

    assert session.closed is True
    assert "cell-test" not in ac._session_registry


def test_mission_spec_requires_iteration_field_default() -> None:
    """The planner contract field is additive and defaults to the trivial path."""
    spec = MissionSpecification(
        outcome="x", scope=["a"], constraints=["b"], decisions=["c"],
        tasks=[WBSStep(step_number=1, action="edit_file", target_file="a.py", description="d")],
        checks=["e"],
    )
    assert spec.tasks[0].requires_iteration is False
