# tests/test_phase7_19_checkpoint_gate.py
"""Phase 7.19 — Autonomous Execution Cell & Glass-Box Telemetry — Checkpoint Gate.

Single E2E certification that the Phase 7.19 pillars hold together against their
shipped entry points. Test-only: it imports and invokes production code, asserting
the one load-bearing invariant per gate row — it does not re-run the dedicated
suites, and it modifies no production logic.

Async cases use asyncio.run() — no pytest-asyncio dependency.

Backend rows certified here (pytest-assertable):
  SESS1       session created in _session_registry while non-terminal;
              removed by _close_cell on terminal exit
  PTY1        write_session_stdin delivers bytes; interrupt_session signals;
              both False for unknown id
  SYNC1       _compute_edit: new-file creation succeeds; anchor miss raises PatchError
              (the cell's outer handler concedes gracefully — no crash, no silent skip)
  CELL1       green exit (exit_code=0) -> route_after_cell == "contract_guard"
  CELL2       delta["agentic_iteration"] increments; trajectory record carries
              matching iteration index
  GOV1        step axis: step>=max_steps -> AxisExhausted.STEPS; step<max_steps -> None
  GOV2        time axis: elapsed_s>=max_elapsed_s -> AxisExhausted.TIME
  GOV3        cost axis: cost_usd>=max_cost_usd -> AxisExhausted.TOKENS
  WS1         all 4 ServerCell* events validate through WebSocketMessage union
  MCTS-LIVE   select_candidate_via_mcts callable in brain.agentic_cell (positive
              evidence); spine (engine.py + coder.py) imports no brain.mcts
              (DEBT-009 re-certified)
  CHECKLIST1  emit_graph_mutation produces ServerGraphMutationEvent JSON with
              correct step_number and new_status
  SEED1       _WBS_SEED_DIRECTIVE is non-empty and carries "EXISTING PLAN AS SEED"

Frontend rows (npm run compile + smoke; no pytest functions):
  AuditWidgets  CellAuditWidget.tsx collapsible accordion per iteration (7.19.5)
  SEND-STOP     Composer button mutates Send/Stop on streaming flag (7.19.6)
  CHECKLIST-UI  ExecutionChecklist.tsx checkbox->spinner->checkmark from
                server_graph_mutation (7.19.7)
  EXPLAIN1      GFM table in assistant answer renders as .ws-md-table (7.19.7)
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import brain.agentic_cell as ac
from brain.agentic_cell import (
    _CellSession,
    _compute_edit,
    ToolCall,
    route_after_cell,
    run_agentic_cell_node,
    select_candidate_via_mcts,
)
from brain.iteration_governor import AxisExhausted, check_governor
from api.websocket_manager import ConnectionManager
from api.ws_contracts import WebSocketMessage
from agents.planner import _WBS_SEED_DIRECTIVE
from core.workspace_sync import SyncSurface, _raw_sha256
from pydantic import TypeAdapter

_PKG_ROOT = Path(__file__).resolve().parent.parent


# ── Stubs (mirrors test_phase7_19_4_cell_dispatcher.py) ─────────────────────


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
    def __init__(
        self,
        exit_codes: List[int],
        output_chunks: Optional[List[List[bytes]]] = None,
    ) -> None:
        self._exit_codes = list(exit_codes)
        self._output_chunks: List[List[bytes]] = output_chunks or []
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False
        self.killed = False
        self.closed = False

    async def start(self) -> None:
        return None

    async def run(self, _cmd: str, *, timeout_s: float) -> int:
        chunks = self._output_chunks.pop(0) if self._output_chunks else [b"ok"]
        for chunk in chunks:
            await self._q.put(chunk)
        code = (
            self._exit_codes.pop(0)
            if len(self._exit_codes) > 1
            else (self._exit_codes[0] if self._exit_codes else 0)
        )
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

    async def open_session(
        self, *, cwd: str, env_whitelist: Dict[str, str], **_: Any
    ) -> _StubSession:
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


def _config(
    adapter: _StubAdapter, reasoner: ac.CellReasoner, **extra: Any
) -> Dict[str, Any]:
    configurable: Dict[str, Any] = {"cell_adapter": adapter, "cell_reasoner": reasoner}
    configurable.update(extra)
    return {"configurable": configurable}


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "gate-7-19",
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


class _FakeWS:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# ── SESS1 — session lifecycle: persists while looping, removed on terminal exit ─


def test_sess1_session_lifecycle() -> None:
    async def body() -> None:
        # Non-terminal (exit_code=1, steps cap far away): session stays in registry
        t1 = "sess1-loop"
        session1 = _StubSession([1])
        adapter1 = _StubAdapter(session1, _StubSyncSurface())
        reasoner1 = _reasoner_from(
            [[ToolCall(name="run_terminal", args={"command": "false"})]]
        )
        await run_agentic_cell_node(
            _base_state(task_id=t1), _config(adapter1, reasoner1, cell_max_steps=999)
        )
        assert t1 in ac._session_registry, "session must persist while status == continue"
        await ac._close_cell(t1)  # explicit teardown for test isolation

        # Terminal (exit_code=0): session closed in finally block
        t2 = "sess1-term"
        session2 = _StubSession([0], output_chunks=[[b"ok"]])
        adapter2 = _StubAdapter(session2, _StubSyncSurface())
        reasoner2 = _reasoner_from(
            [[ToolCall(name="run_terminal", args={"command": "true"})]]
        )
        await run_agentic_cell_node(
            _base_state(task_id=t2), _config(adapter2, reasoner2)
        )
        assert t2 not in ac._session_registry, "session must be removed on terminal exit"

    asyncio.run(body())


# ── PTY1 — write_session_stdin delivers; interrupt_session signals; False if absent ─


def test_pty1_stdin_and_interrupt() -> None:
    received: List[bytes] = []

    class _InstrSession(_StubSession):
        interrupted_count: int = 0

        async def write_stdin(self, data: bytes) -> None:
            received.append(data)

        async def interrupt(self) -> None:  # type: ignore[override]
            self.interrupted_count += 1

    async def body() -> None:
        sess = _InstrSession(exit_codes=[0])
        ac._session_registry["pty1"] = _CellSession(
            session=sess, surface=_StubSyncSurface()
        )

        result = await ac.write_session_stdin("pty1", b"y\n")
        assert result is True
        assert received == [b"y\n"]

        result = await ac.interrupt_session("pty1")
        assert result is True
        assert sess.interrupted_count == 1

        assert await ac.write_session_stdin("unknown-id", b"x") is False
        assert await ac.interrupt_session("unknown-id") is False

        del ac._session_registry["pty1"]

    asyncio.run(body())


# ── SYNC1 — VFS edit contract: new-file creation + anchor-miss raises PatchError ─


def test_sync1_compute_edit_new_file_and_anchor_miss() -> None:
    from core.exceptions import PatchError

    # New-file creation: empty base + empty search → returns the replace content
    created = _compute_edit("", "new.py", "", "x = 1\n")
    assert created == "x = 1\n", "empty-anchor create-file must return replace content"

    # Anchor not found: PatchError propagates (the node's outer handler concedes gracefully)
    raised = False
    try:
        _compute_edit("existing content\n", "f.py", "ANCHOR_NOT_IN_FILE", "y = 2\n")
    except PatchError:
        raised = True
    assert raised, "PatchError must propagate from _compute_edit when anchor is absent"


# ── CELL1 — green exit routes to contract_guard (loop exits) ─────────────────


def test_cell1_green_exit_routes_to_contract_guard() -> None:
    async def body() -> None:
        session = _StubSession([0], output_chunks=[[b"all ok"]])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from(
            [[ToolCall(name="run_terminal", args={"command": "pytest -q"})]]
        )
        state = _base_state(task_id="cell1-task")
        delta = await run_agentic_cell_node(state, _config(adapter, reasoner))
        _apply(state, delta)

        traj = state.get("agentic_trajectory", [])
        assert any(
            r.get("status") == "green" for r in traj if isinstance(r, dict)
        ), "green exit must produce a 'green' trajectory record"
        assert route_after_cell(state) == "contract_guard"

    asyncio.run(body())


# ── CELL2 — trajectory carries iteration index; counter increments ────────────


def test_cell2_trajectory_carries_iteration_and_counter_increments() -> None:
    async def body() -> None:
        session = _StubSession([1])
        adapter = _StubAdapter(session, _StubSyncSurface())
        reasoner = _reasoner_from(
            [[ToolCall(name="run_terminal", args={"command": "false"})]]
        )
        state = _base_state(task_id="cell2-task", agentic_iteration=2)
        delta = await run_agentic_cell_node(
            state, _config(adapter, reasoner, cell_max_steps=999)
        )
        await ac._close_cell("cell2-task")

        assert delta["agentic_iteration"] == 3, "counter must increment from 2 to 3"
        records = [
            r for r in delta.get("agentic_trajectory", [])
            if isinstance(r, dict) and "iteration" in r
        ]
        assert records, "trajectory must carry at least one iteration record"
        assert records[0]["iteration"] == 2, "record must reflect the input iteration index"

    asyncio.run(body())


# ── GOV1 — step axis trips at the limit ──────────────────────────────────────


def test_gov1_step_axis_trips_at_limit() -> None:
    assert check_governor(
        step=5, cost_usd=0.0, elapsed_s=0.0,
        max_steps=5, max_cost_usd=100.0, max_elapsed_s=3600.0,
    ) is AxisExhausted.STEPS
    assert check_governor(
        step=4, cost_usd=0.0, elapsed_s=0.0,
        max_steps=5, max_cost_usd=100.0, max_elapsed_s=3600.0,
    ) is None


# ── GOV2 — time axis trips at the limit ──────────────────────────────────────


def test_gov2_time_axis_trips_at_limit() -> None:
    assert check_governor(
        step=1, cost_usd=0.0, elapsed_s=120.0,
        max_steps=100, max_cost_usd=100.0, max_elapsed_s=60.0,
    ) is AxisExhausted.TIME


# ── GOV3 — cost axis trips at the limit ──────────────────────────────────────


def test_gov3_cost_axis_trips_at_limit() -> None:
    assert check_governor(
        step=1, cost_usd=1.5, elapsed_s=0.0,
        max_steps=100, max_cost_usd=1.0, max_elapsed_s=3600.0,
    ) is AxisExhausted.TOKENS


# ── WS1 — all 4 cell typed events validate through the WebSocketMessage union ─


def test_ws1_cell_events_validate_through_websocket_message_union() -> None:
    adapter: TypeAdapter[WebSocketMessage] = TypeAdapter(WebSocketMessage)
    frames = [
        {
            "event_type": "server_cell_tool_start",
            "data": {
                "session_id": "s", "iteration": 0,
                "tool_name": "run_terminal", "args_scrubbed": {},
            },
        },
        {
            "event_type": "server_cell_pty_chunk",
            "data": {
                "session_id": "s", "iteration": 0,
                "text": "output line\n", "is_stderr": False,
            },
        },
        {
            "event_type": "server_cell_ast_diff",
            "data": {
                "session_id": "s", "iteration": 0,
                "path": "src/f.py", "search": "", "replace": "x = 1",
            },
        },
        {
            "event_type": "server_cell_governor_tick",
            "data": {
                "session_id": "s", "step": 1,
                "cost_usd": 0.01, "elapsed_s": 0.5, "axis": None,
            },
        },
    ]
    for frame in frames:
        result = adapter.validate_python(frame)
        assert result.event_type == frame["event_type"], (
            f"union dispatch failed for {frame['event_type']}"
        )


# ── MCTS-LIVE — cell is MCTS's live home; spine stays MCTS-free (DEBT-009) ───


def test_mcts_live_cell_is_live_home_and_spine_stays_free() -> None:
    # Positive evidence: the function lives in agentic_cell (not just a deferred import)
    assert callable(select_candidate_via_mcts), (
        "select_candidate_via_mcts must be importable and callable from brain.agentic_cell"
    )

    # Negative invariant: the single-shot spine must never wire in the MCTS tree
    def _module_imports(rel: str) -> List[str]:
        tree = ast.parse((_PKG_ROOT / rel).read_text(encoding="utf-8"))
        names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        return names

    for rel in ("brain/engine.py", "agents/coder.py"):
        imported = _module_imports(rel)
        assert not any(name.startswith("brain.mcts") for name in imported), (
            f"{rel} wires the MCTS tree into the single-shot spine"
        )


# ── CHECKLIST1 — emit_graph_mutation produces a valid ServerGraphMutationEvent ─


def test_checklist1_emit_graph_mutation_produces_valid_event() -> None:
    ws_type_adapter: TypeAdapter[WebSocketMessage] = TypeAdapter(WebSocketMessage)
    ws = _FakeWS()
    mgr = ConnectionManager()
    mgr.active_connections["chk-sess"] = ws  # type: ignore[assignment]

    asyncio.run(mgr.emit_graph_mutation("chk-sess", step_number=3, new_status="completed"))

    assert len(ws.sent) == 1, "emit_graph_mutation must send exactly one message"
    raw = json.loads(ws.sent[0])
    assert raw["event_type"] == "server_graph_mutation"
    assert raw["data"]["step_number"] == 3
    assert raw["data"]["new_status"] == "completed"
    # Also validate through the full union discriminator
    ws_type_adapter.validate_python(raw)


# ── SEED1 — _WBS_SEED_DIRECTIVE constant present and carries key phrase ───────


def test_seed1_wbs_seed_directive_present_and_non_empty() -> None:
    assert _WBS_SEED_DIRECTIVE, "_WBS_SEED_DIRECTIVE must be a non-empty string"
    assert "EXISTING PLAN AS SEED" in _WBS_SEED_DIRECTIVE, (
        "_WBS_SEED_DIRECTIVE must carry the key instruction phrase"
    )
