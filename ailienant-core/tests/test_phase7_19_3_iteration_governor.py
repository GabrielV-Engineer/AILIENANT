"""Phase 7.19.3 DoD — Multi-Axis Iteration Governor (Circuit Breaker).

Tests the three-axis governor that replaces the single-axis step ceiling from 7.19.2:
  - check_governor pure function (one test per axis)
  - Integration through run_agentic_cell_node (one test per axis + happy path + axis field)
  - Cost delta wired into current_cost_usd (finops integration)

Async cases use asyncio.run so the stub session lives on one event loop across iterations.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall, route_after_cell, run_agentic_cell_node
from brain.iteration_governor import AxisExhausted, check_governor
from core.workspace_sync import SyncSurface, _raw_sha256


# ── Stubs (self-contained — mirrors the 7.19.2 test pattern) ─────────────────────────────


class StubSyncSurface(SyncSurface):
    def __init__(self, initial: Optional[Dict[str, bytes]] = None) -> None:
        self._files: Dict[str, bytes] = dict(initial or {})

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}


class StubSession:
    supports_sessions = True

    def __init__(self, exit_codes: List[int], outputs: List[bytes]) -> None:
        self._exit_codes = list(exit_codes)
        self._outputs = list(outputs)
        self._q: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._closed = False
        self.closed = False

    async def start(self) -> None:
        return None

    async def run(self, command: str, *, timeout_s: float) -> int:
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
        self.closed = True
        self._closed = True

    async def close(self) -> None:
        self.closed = True
        self._closed = True


class StubAdapter:
    supports_sessions = True

    def __init__(self, session: StubSession, surface: StubSyncSurface) -> None:
        self._session = session
        self._surface = surface

    async def open_session(self, *, cwd: str, env_whitelist: Dict[str, str], **_: Any) -> StubSession:
        return self._session

    def get_sync_surface(self, cwd: str) -> StubSyncSurface:
        return self._surface


def _reasoner_from(scripts: List[List[ToolCall]]) -> ac.CellReasoner:
    calls: Dict[str, int] = {"i": 0}

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
        "task_id": "gov-test",
        "user_input": "fix the tests",
        "workspace_root": "/work",
        "vfs_buffer": {},
        "agentic_iteration": 0,
        "agentic_trajectory": [],
        "session_permission_mode": "AUTO",
        "mission_spec": None,
        "current_cost_usd": 0.0,
        "max_budget_usd": 100.0,
    }
    state.update(overrides)
    return state


def _apply(state: Dict[str, Any], delta: Dict[str, Any]) -> None:
    for key, value in delta.items():
        if key in ("agentic_trajectory", "permission_audit_log", "security_flags", "errors"):
            state[key] = (state.get(key) or []) + value
        elif key in ("vfs_buffer", "pending_contents"):
            merged = dict(state.get(key) or {})
            merged.update(value)
            state[key] = merged
        elif key == "current_cost_usd":
            # Mirrors the operator.add reducer in the real graph.
            state[key] = float(state.get(key, 0.0)) + float(value)
        else:
            state[key] = value


async def _drive(state: Dict[str, Any], config: Dict[str, Any], max_visits: int = 30) -> int:
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


# ── Pure unit tests for check_governor ───────────────────────────────────────────────────


def test_check_governor_pure_steps() -> None:
    """step >= max_steps → AxisExhausted.STEPS."""
    assert check_governor(step=6, cost_usd=0.0, elapsed_s=0.0, max_steps=6, max_cost_usd=100.0, max_elapsed_s=300.0) is AxisExhausted.STEPS
    assert check_governor(step=5, cost_usd=0.0, elapsed_s=0.0, max_steps=6, max_cost_usd=100.0, max_elapsed_s=300.0) is None


def test_check_governor_pure_tokens() -> None:
    """cost_usd >= max_cost_usd → AxisExhausted.TOKENS."""
    assert check_governor(step=0, cost_usd=2.0, elapsed_s=0.0, max_steps=10, max_cost_usd=2.0, max_elapsed_s=300.0) is AxisExhausted.TOKENS
    assert check_governor(step=0, cost_usd=1.99, elapsed_s=0.0, max_steps=10, max_cost_usd=2.0, max_elapsed_s=300.0) is None


def test_check_governor_pure_time() -> None:
    """elapsed_s >= max_elapsed_s → AxisExhausted.TIME."""
    assert check_governor(step=0, cost_usd=0.0, elapsed_s=300.0, max_steps=10, max_cost_usd=100.0, max_elapsed_s=300.0) is AxisExhausted.TIME
    assert check_governor(step=0, cost_usd=0.0, elapsed_s=299.9, max_steps=10, max_cost_usd=100.0, max_elapsed_s=300.0) is None


# ── Integration tests through run_agentic_cell_node ──────────────────────────────────────


def test_steps_axis_exhausted() -> None:
    """cell_max_steps=2, never-converging → 2 visits, axis == budget_steps."""
    session = StubSession(exit_codes=[1], outputs=[b"still failing\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    visits = asyncio.run(_drive(state, _config(adapter, reasoner, cell_max_steps=2)))

    assert visits == 2
    budget_records = [r for r in state["agentic_trajectory"] if r.get("status") == "budget"]
    assert budget_records, "expected at least one budget record"
    assert budget_records[-1].get("axis") == "budget_steps"


def test_token_axis_exhausted() -> None:
    """cell_max_cost_usd=0.0, never-converging → trips on first iter, axis == budget_tokens."""
    session = StubSession(exit_codes=[1], outputs=[b"failing\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    # Any non-zero cost estimate exceeds the 0.0 ceiling.
    visits = asyncio.run(_drive(state, _config(adapter, reasoner, cell_max_cost_usd=0.0)))

    assert visits == 1
    budget_records = [r for r in state["agentic_trajectory"] if r.get("status") == "budget"]
    assert budget_records[-1].get("axis") == "budget_tokens"


def test_time_axis_exhausted() -> None:
    """cell_max_elapsed_s=0.0, never-converging → trips immediately, axis == budget_time."""
    session = StubSession(exit_codes=[1], outputs=[b"failing\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    # Any elapsed time > 0 exceeds the 0.0 ceiling (start_time set before iteration runs).
    visits = asyncio.run(_drive(state, _config(adapter, reasoner, cell_max_elapsed_s=0.0)))

    assert visits == 1
    budget_records = [r for r in state["agentic_trajectory"] if r.get("status") == "budget"]
    assert budget_records[-1].get("axis") == "budget_time"


def test_happy_path_before_cap() -> None:
    """exit-0 stub, generous caps → 1 visit, status green, no axis trip."""
    session = StubSession(exit_codes=[0], outputs=[b"1 passed\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    visits = asyncio.run(_drive(state, _config(
        adapter, reasoner,
        cell_max_steps=10, cell_max_cost_usd=100.0, cell_max_elapsed_s=3600.0,
    )))

    assert visits == 1
    last = state["agentic_trajectory"][-1]
    assert last["status"] == "green"
    assert "axis" not in last


def test_concede_delta_has_axis_field() -> None:
    """Any axis trip produces a trajectory record with an 'axis' key."""
    session = StubSession(exit_codes=[1], outputs=[b"failing\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner, cell_max_steps=1)))

    budget_records = [r for r in state["agentic_trajectory"] if r.get("status") == "budget"]
    assert budget_records, "expected a budget record"
    assert "axis" in budget_records[-1]


def test_cost_delta_emitted_in_state() -> None:
    """After one iteration the accumulated current_cost_usd is positive (finops wiring)."""
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    asyncio.run(_drive(state, _config(adapter, reasoner)))

    assert float(state.get("current_cost_usd", 0.0)) > 0.0
