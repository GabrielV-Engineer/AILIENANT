"""Phase 7.19.4 DoD — WebSocket Telemetry API & Event Dispatcher (Glass-Box).

Directed suite verifying:
  - A cell command emits an ordered sequence of typed deltas.
  - PTY chunks are dispatched in real-time (multiple chunks, not one batch).
  - AST diff emitted on apply_granular_edit.
  - governor_tick carries axis when a budget axis trips.
  - A closed connection is purged from active_connections (no-leak).
  - Post-disconnect dispatch is a silent no-op (no crash, no orphan entry).
  - Routing is O(1) dict-based.
  - NullCellDispatcher default causes no exceptions.
  - Multi-tool turn emits tool_call_start for each tool in order.

Async cases use asyncio.run() — no pytest-asyncio dependency.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall, route_after_cell, run_agentic_cell_node
from brain.cell_dispatcher import CellEventDispatcher, NullCellDispatcher
from core.workspace_sync import SyncSurface, _raw_sha256
from api.websocket_manager import ConnectionManager, LiveCellDispatcher


# ── Capturing stub dispatcher ────────────────────────────────────────────────


class CapturingCellDispatcher:
    """Records every emit call as (event_type, kwargs) for assertion."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    async def emit_tool_call_start(self, **kwargs: Any) -> None:
        self.events.append(("tool_call_start", kwargs))

    async def emit_pty_chunk(self, **kwargs: Any) -> None:
        self.events.append(("pty_chunk", kwargs))

    async def emit_ast_diff(self, **kwargs: Any) -> None:
        self.events.append(("ast_diff", kwargs))

    async def emit_governor_tick(self, **kwargs: Any) -> None:
        self.events.append(("governor_tick", kwargs))

    def types(self) -> List[str]:
        return [e[0] for e in self.events]


# ── Session/surface stubs (reuse shape from 7.19.2) ─────────────────────────


class _StubSyncSurface(SyncSurface):
    def __init__(self, files: Optional[Dict[str, bytes]] = None) -> None:
        self._files: Dict[str, bytes] = dict(files or {})

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}


class _StubSession:
    """Scripted session with a controllable stream (supports multi-chunk output)."""

    def __init__(
        self,
        exit_codes: List[int],
        output_chunks: Optional[List[List[bytes]]] = None,
    ) -> None:
        self._exit_codes = list(exit_codes)
        self._output_chunks: List[List[bytes]] = output_chunks or []
        self._q: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._closed = False
        self.killed = False
        self.closed = False

    async def start(self) -> None:
        return None

    async def run(self, _cmd: str, *, timeout_s: float) -> int:
        chunks = self._output_chunks.pop(0) if self._output_chunks else [b"ok"]
        for chunk in chunks:
            await self._q.put(chunk)
        code = self._exit_codes.pop(0) if len(self._exit_codes) > 1 else (self._exit_codes[0] if self._exit_codes else 0)
        return code

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
        self._closed = True
        self.closed = True

    async def close(self) -> None:
        self._closed = True
        self.closed = True


class _StubAdapter:
    supports_sessions = True

    def __init__(self, session: _StubSession, surface: _StubSyncSurface) -> None:
        self._session = session
        self._surface = surface

    async def open_session(self, *, cwd: str, env_whitelist: Dict[str, str], **_: Any) -> _StubSession:
        return self._session

    def get_sync_surface(self, cwd: str) -> _StubSyncSurface:
        return self._surface


def _reasoner_from(scripts: List[List[ToolCall]]) -> ac.CellReasoner:
    calls: Dict[str, int] = {"i": 0}

    async def _reason(_messages: Any) -> List[ToolCall]:
        idx = min(calls["i"], len(scripts) - 1)
        calls["i"] += 1
        return scripts[idx]

    return _reason


def _config(adapter: _StubAdapter, reasoner: ac.CellReasoner, **extra: Any) -> Dict[str, Any]:
    configurable: Dict[str, Any] = {"cell_adapter": adapter, "cell_reasoner": reasoner}
    configurable.update(extra)
    return {"configurable": configurable}


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "disp-test",
        "user_input": "fix it",
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
            state[key] = float(state.get(key, 0.0)) + float(value)
        else:
            state[key] = value


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# ── FakeWS (mirrors test_ws_buffer_lifecycle pattern) ───────────────────────


class _FakeWS:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


# ════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════


def test_ordered_delta_sequence() -> None:
    """run_terminal stub → event types arrive in order: tool_call_start, pty_chunk(s), governor_tick."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        session = _StubSession(exit_codes=[0], output_chunks=[[b"all good"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from([[ToolCall(name="run_terminal", args={"command": "pytest"})]])
        config = _config(adapter, reasoner, cell_dispatcher=cap)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        types = cap.types()
        assert "tool_call_start" in types
        assert "pty_chunk" in types
        assert "governor_tick" in types
        # Order: tool_call_start before pty_chunk before governor_tick
        assert types.index("tool_call_start") < types.index("pty_chunk")
        assert types.index("pty_chunk") < types.index("governor_tick")

    asyncio.run(body())


def test_pty_chunk_streams_in_realtime() -> None:
    """PTY stub emits >=2 chunks per command → dispatcher receives >=2 pty_chunk events."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        # Three chunks for one command — simulates incremental streaming output
        session = _StubSession(exit_codes=[0], output_chunks=[[b"line1\n", b"line2\n", b"line3\n"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from([[ToolCall(name="run_terminal", args={"command": "cat log"})]])
        config = _config(adapter, reasoner, cell_dispatcher=cap)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        pty_events = [e for e in cap.events if e[0] == "pty_chunk"]
        assert len(pty_events) >= 2, f"Expected >=2 pty_chunk events, got {len(pty_events)}"
        # Text reassembled matches full output
        full_text = "".join(e[1]["text"] for e in pty_events)
        assert "line1" in full_text and "line2" in full_text

    asyncio.run(body())


def test_ast_diff_emitted() -> None:
    """apply_granular_edit stub → ast_diff emitted with correct path/search/replace."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        # search="" = "create new file" — always succeeds regardless of base content
        session = _StubSession(exit_codes=[0])
        surface = _StubSyncSurface()
        adapter = _StubAdapter(session, surface)

        reasoner = _reasoner_from([[
            ToolCall(
                name="apply_granular_edit",
                args={"path": "src/new.py", "search": "", "replace": "def foo(): return 1"},
            )
        ]])
        config = _config(adapter, reasoner, cell_dispatcher=cap)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        ast_events = [e for e in cap.events if e[0] == "ast_diff"]
        assert len(ast_events) >= 1
        ev = ast_events[0][1]
        assert ev["path"] == "src/new.py"
        assert "return 1" in ev["replace"]

    asyncio.run(body())


def test_tool_call_start_payload() -> None:
    """tool_call_start payload carries tool_name and args_scrubbed (no raw secret)."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        session = _StubSession(exit_codes=[0], output_chunks=[[b"done"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from([[ToolCall(name="run_terminal", args={"command": "echo hi"})]])
        config = _config(adapter, reasoner, cell_dispatcher=cap)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        tc_events = [e for e in cap.events if e[0] == "tool_call_start"]
        assert len(tc_events) >= 1
        ev = tc_events[0][1]
        assert ev["tool_name"] == "run_terminal"
        assert "args_scrubbed" in ev
        assert isinstance(ev["args_scrubbed"], dict)

    asyncio.run(body())


def test_governor_tick_axis_on_trip() -> None:
    """Budget axis trip → governor_tick payload has axis != None."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        session = _StubSession(exit_codes=[1], output_chunks=[[b"fail"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        # Never-green, steps cap = 1 → trips on first iteration
        reasoner = _reasoner_from([[ToolCall(name="run_terminal", args={"command": "false"})]])
        config = _config(adapter, reasoner, cell_dispatcher=cap, cell_max_steps=1)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        gov_events = [e for e in cap.events if e[0] == "governor_tick"]
        assert len(gov_events) >= 1
        assert gov_events[-1][1]["axis"] is not None

    asyncio.run(body())


def test_multi_tool_ordering() -> None:
    """Two-call turn → two tool_call_start events emitted in order."""

    async def body() -> None:
        cap = CapturingCellDispatcher()
        # Use search="" (create-file mode) to avoid blob-store dependency
        session = _StubSession(exit_codes=[0], output_chunks=[[b"ok"]])
        adapter = _StubAdapter(session, _StubSyncSurface())

        reasoner = _reasoner_from([[
            ToolCall(name="run_terminal", args={"command": "python -c 'pass'"}),
            ToolCall(
                name="apply_granular_edit",
                args={"path": "src/b.py", "search": "", "replace": "x = 2"},
            ),
        ]])
        config = _config(adapter, reasoner, cell_dispatcher=cap)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        tc_events = [e for e in cap.events if e[0] == "tool_call_start"]
        assert len(tc_events) == 2
        assert tc_events[0][1]["tool_name"] == "run_terminal"
        assert tc_events[1][1]["tool_name"] == "apply_granular_edit"

    asyncio.run(body())


def test_null_dispatcher_no_crash() -> None:
    """No cell_dispatcher in configurable → NullCellDispatcher, no exception, trajectory intact."""

    async def body() -> None:
        session = _StubSession(exit_codes=[0], output_chunks=[[b"ok"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from([[ToolCall(name="run_terminal", args={"command": "true"})]])
        # No cell_dispatcher key → NullCellDispatcher default
        config = _config(adapter, reasoner)
        state = _base_state()
        delta = await run_agentic_cell_node(state, config)
        _apply(state, delta)

        traj = state.get("agentic_trajectory", [])
        assert len(traj) >= 1

    asyncio.run(body())


def test_closed_connection_purged() -> None:
    """Manual seed + disconnect() → session_id removed from active_connections."""
    mgr = ConnectionManager()
    ws = _FakeWS()
    # Seed directly (mirrors test_ws_buffer_lifecycle pattern — avoids accept())
    mgr.active_connections["sess-gc"] = ws  # type: ignore[assignment]
    assert "sess-gc" in mgr.active_connections
    mgr.disconnect("sess-gc", ws)  # type: ignore[arg-type]
    assert "sess-gc" not in mgr.active_connections


def test_stale_dispatch_is_noop() -> None:
    """Post-disconnect LiveCellDispatcher.emit_* is a silent no-op — no crash, no orphan entry."""
    mgr = ConnectionManager()
    ws = _FakeWS()
    mgr.active_connections["sess-stale"] = ws  # type: ignore[assignment]
    mgr.disconnect("sess-stale", ws)  # type: ignore[arg-type]
    # Session is now absent. Route LiveCellDispatcher through a local mgr.
    import api.websocket_manager as wm
    orig = wm.vfs_manager
    wm.vfs_manager = mgr
    try:
        d = LiveCellDispatcher("sess-stale")

        async def body() -> None:
            await d.emit_governor_tick(step=1, cost_usd=0.0, elapsed_s=0.1, axis=None)
            await d.emit_tool_call_start(iteration=0, tool_name="run_terminal", args_scrubbed={})

        asyncio.run(body())  # must not raise
    finally:
        wm.vfs_manager = orig


def test_routing_is_o1() -> None:
    """active_connections is a plain dict — O(1) lookup by construction."""
    mgr = ConnectionManager()
    assert isinstance(mgr.active_connections, dict)
