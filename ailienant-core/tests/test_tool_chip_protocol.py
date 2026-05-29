# tests/test_tool_chip_protocol.py
"""Phase 7.11.6 DoD — Rich Tool Chips backend protocol (ADR-706 §4.5f).

Six tests covering the backend-side contract for the tool-chip pipeline:

  1. `execute_tracked_tool` registers a spec and broadcasts the
     (start → stream_chunk → result) sequence in order.
  2. `retry_tool_call` looks up the registry and re-invokes the original
     tool with the SAME args (exact-replay semantics).
  3. `retry_tool_call(unknown_id)` returns False and emits zero broadcasts.
  4. `cleanup_session` purges every entry for the given session_id (and only
     those entries — entries for other sessions survive).
  5. Pydantic round-trip for all six new WS event payloads.
  6. `side_effect_free` flag is faithfully stored on the spec (default False
     for sandbox_bash; True when callers explicitly opt in).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from api.ws_contracts import (
    ClientInvokeTrackedBashEvent, ClientInvokeTrackedBashPayload,
    ClientRetryToolEvent, ClientRetryToolPayload,
    ServerToolStartEvent, ToolStartPayload,
    ServerToolStreamChunkEvent, ToolStreamChunkPayload,
    ServerToolResultEvent, ToolResultPayload,
    ServerToolDepGraphEvent, ToolDepGraphPayload,
)
from core.task_service import TaskService, ToolCallSpec

pytestmark = pytest.mark.anyio


# ──────────────────────────────────────────────────────────────────────────────
# Shared adapter fake — matches what `core.sandbox.get_active_adapter()` returns.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeSandboxResult:
    stdout: str
    stderr: str
    exit_code: int


class _FakeAdapter:
    """Records every `.execute(...)` call and returns a configurable result."""

    def __init__(self, exit_code: int = 0, stdout: str = "hello\n", stderr: str = "") -> None:
        self.calls: list[Dict[str, Any]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Any,
    ) -> _FakeSandboxResult:
        self.calls.append({
            "command": command, "timeout_s": timeout_s, "cwd": cwd,
        })
        return _FakeSandboxResult(
            stdout=self._stdout, stderr=self._stderr, exit_code=self._exit_code,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Register + broadcast in order
# ──────────────────────────────────────────────────────────────────────────────

async def test_execute_tracked_tool_registers_and_broadcasts_in_order() -> None:
    """``execute_tracked_tool`` must (a) populate the registry with a spec,
    (b) broadcast tool_start, tool_stream_chunk, tool_result in that order,
    (c) return a populated spec with status=success on exit_code=0.
    """
    ts = TaskService()
    adapter = _FakeAdapter(exit_code=0, stdout="hello world\n", stderr="")

    with patch("core.sandbox.get_active_adapter", return_value=adapter), \
         patch("core.task_service.vfs_manager") as mock_vfs:
        mock_vfs.broadcast_tool_start = AsyncMock()
        mock_vfs.broadcast_tool_stream_chunk = AsyncMock()
        mock_vfs.broadcast_tool_result = AsyncMock()

        spec = await ts.execute_tracked_tool(
            session_id="sess-1",
            tool_name="sandbox_bash",
            args={"command": "echo hello"},
            side_effect_free=False,
        )

    assert spec.tool_name == "sandbox_bash"
    assert spec.status == "success"
    assert spec.exit_code == 0
    assert spec.duration_ms is not None
    # Registry was populated with the (session_id, tool_call_id) key.
    assert (("sess-1", spec.tool_call_id)) in ts._tool_call_registry  # type: ignore[operator]

    # Broadcast ORDER: start → stream_chunk → result. We assert on call_count and
    # the relative ordering by inspecting `mock_vfs.method_calls`.
    method_names = [c[0] for c in mock_vfs.method_calls]
    start_idx  = method_names.index("broadcast_tool_start")
    chunk_idx  = method_names.index("broadcast_tool_stream_chunk")
    result_idx = method_names.index("broadcast_tool_result")
    assert start_idx < chunk_idx < result_idx, (
        f"events out of order: {method_names}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Retry replays the same args
# ──────────────────────────────────────────────────────────────────────────────

async def test_retry_tool_call_re_invokes_with_original_args() -> None:
    """Exact-replay semantics: retry must call the adapter a SECOND time with
    the SAME command string the original invocation used."""
    ts = TaskService()
    adapter = _FakeAdapter(exit_code=0, stdout="ok\n")

    with patch("core.sandbox.get_active_adapter", return_value=adapter), \
         patch("core.task_service.vfs_manager") as mock_vfs:
        mock_vfs.broadcast_tool_start = AsyncMock()
        mock_vfs.broadcast_tool_stream_chunk = AsyncMock()
        mock_vfs.broadcast_tool_result = AsyncMock()

        first = await ts.execute_tracked_tool(
            session_id="sess-1",
            tool_name="sandbox_bash",
            args={"command": "ls /tmp", "timeout_sec": 5.0},
        )
        ok = await ts.retry_tool_call("sess-1", first.tool_call_id)

    assert ok is True
    # Adapter must have been called exactly twice with the same command.
    assert len(adapter.calls) == 2
    assert adapter.calls[0]["command"] == "ls /tmp"
    assert adapter.calls[1]["command"] == "ls /tmp"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Retry of an unknown id is a no-op
# ──────────────────────────────────────────────────────────────────────────────

async def test_retry_tool_call_unknown_id_returns_false() -> None:
    ts = TaskService()
    with patch("core.task_service.vfs_manager") as mock_vfs:
        mock_vfs.broadcast_tool_start = AsyncMock()
        mock_vfs.broadcast_tool_stream_chunk = AsyncMock()
        mock_vfs.broadcast_tool_result = AsyncMock()

        ok = await ts.retry_tool_call("sess-1", "deadbeef")

    assert ok is False
    # No broadcasts at all for the unknown id.
    mock_vfs.broadcast_tool_start.assert_not_called()
    mock_vfs.broadcast_tool_stream_chunk.assert_not_called()
    mock_vfs.broadcast_tool_result.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 4. cleanup_session is session-scoped
# ──────────────────────────────────────────────────────────────────────────────

def test_cleanup_session_purges_only_matching_entries() -> None:
    ts = TaskService()
    # Seed the registry directly — we don't need the broadcasts for this test.
    for sid, tcid in [
        ("sess-A", "id-1"), ("sess-A", "id-2"),
        ("sess-B", "id-3"),
    ]:
        ts._tool_call_registry[(sid, tcid)] = ToolCallSpec(  # type: ignore[index]
            tool_call_id=tcid,
            tool_name="sandbox_bash",
            args={"command": "x"},
            side_effect_free=False,
            invoked_at=0.0,
        )

    purged = ts.cleanup_session("sess-A")
    assert purged == 2
    # Only sess-B's entry survives.
    remaining = list(ts._tool_call_registry.keys())  # type: ignore[arg-type]
    assert remaining == [("sess-B", "id-3")]


# ──────────────────────────────────────────────────────────────────────────────
# 5. Pydantic round-trip for all six new events
# ──────────────────────────────────────────────────────────────────────────────

def test_tool_event_payloads_round_trip() -> None:
    """Every new event in ws_contracts.py must round-trip cleanly through
    Pydantic's validate ↔ dump cycle so the discriminated union resolves on
    both sides of the WS."""
    events: list[Any] = [
        ServerToolStartEvent(data=ToolStartPayload(
            session_id="s", tool_call_id="t", tool_name="sandbox_bash",
            args={"command": "echo hi"}, side_effect_free=False,
            invoked_at=1234567.0,
        )),
        ServerToolStreamChunkEvent(data=ToolStreamChunkPayload(
            session_id="s", tool_call_id="t", chunk="hello", is_stderr=False,
        )),
        ServerToolResultEvent(data=ToolResultPayload(
            session_id="s", tool_call_id="t", status="success",
            exit_code=0, duration_ms=42,
        )),
        ServerToolDepGraphEvent(data=ToolDepGraphPayload(
            session_id="s", tool_call_id="t",
            nodes=[{"id": "a.py", "label": "a.py"}, {"id": "b.py", "label": "b.py"}],
            edges=[{"from": "a.py", "to": "b.py"}],
        )),
        ClientRetryToolEvent(data=ClientRetryToolPayload(
            session_id="s", tool_call_id="t",
        )),
        ClientInvokeTrackedBashEvent(data=ClientInvokeTrackedBashPayload(
            session_id="s", command="ls", timeout_sec=10.0, working_dir=None,
        )),
    ]
    for ev in events:
        raw = ev.model_dump_json()
        # `type(ev).model_validate_json(raw)` returns the exact subclass; cast
        # to Any so mypy doesn't see only the BaseModel base in the loop var.
        round_trip: Any = type(ev).model_validate_json(raw)
        assert round_trip.event_type == ev.event_type, (
            f"event_type drift on {type(ev).__name__}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 6. side_effect_free flag is preserved in the spec
# ──────────────────────────────────────────────────────────────────────────────

async def test_side_effect_free_flag_is_recorded_on_the_spec() -> None:
    """Tools opt-in to ``side_effect_free=True`` to skip the retry-confirmation
    toast on the frontend. The registry must faithfully store this flag so
    that ``retry_tool_call`` passes it back through to the new spec."""
    ts = TaskService()
    adapter = _FakeAdapter(exit_code=0)
    with patch("core.sandbox.get_active_adapter", return_value=adapter), \
         patch("core.task_service.vfs_manager") as mock_vfs:
        mock_vfs.broadcast_tool_start = AsyncMock()
        mock_vfs.broadcast_tool_stream_chunk = AsyncMock()
        mock_vfs.broadcast_tool_result = AsyncMock()

        spec_dangerous = await ts.execute_tracked_tool(
            session_id="s", tool_name="sandbox_bash",
            args={"command": "echo a"}, side_effect_free=False,
        )
        spec_safe = await ts.execute_tracked_tool(
            session_id="s", tool_name="sandbox_bash",
            args={"command": "echo b"}, side_effect_free=True,
        )

    assert spec_dangerous.side_effect_free is False
    assert spec_safe.side_effect_free is True
    # The registry entries reflect the flag exactly.
    stored_d = ts._tool_call_registry[("s", spec_dangerous.tool_call_id)]  # type: ignore[index]
    stored_s = ts._tool_call_registry[("s", spec_safe.tool_call_id)]      # type: ignore[index]
    assert stored_d.side_effect_free is False
    assert stored_s.side_effect_free is True
