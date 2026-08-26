# ailienant-core/tests/test_execution_provenance.py
#
# Covers the Glass-Box Timeline's execution-detail channel: the marker+detail
# pair record_execution (core/exec_log.py) emits through the turn-scoped
# ActivitySink (core/activity_context.py). Hermetic — a fake adapter stands in
# for a real sandbox tier, and a recording sink stands in for the WebSocket.

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import core.exec_log as xl
from core.activity_context import (
    bind_activity_sink,
    current_activity_sink,
    reset_activity_sink,
)
from core.sandbox import SandboxResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_ring():
    xl._reset_for_tests()
    yield
    xl._reset_for_tests()


def _res(exit_code: int = 0, stdout: str = "", stderr: str = "") -> SandboxResult:
    return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class _RecordingSink:
    """Captures every marker/blocked/detail call, in order, for assertion."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
        self.calls.append({"op": "marker", "ref": ref, "target": target})

    async def emit_blocked(self, *, target: str) -> None:
        self.calls.append({"op": "blocked", "target": target})

    async def emit_detail(
        self, *, ref: str, source: str, cwd: Optional[str],
        initiator: Optional[str], stdout: Optional[str], stderr: Optional[str],
        exit_code: Optional[int], duration_ms: Optional[float], truncated: bool,
        error: Optional[str],
    ) -> None:
        self.calls.append({
            "op": "detail", "ref": ref, "source": source, "cwd": cwd,
            "initiator": initiator, "stdout": stdout, "stderr": stderr,
            "exit_code": exit_code, "duration_ms": duration_ms,
            "truncated": truncated, "error": error,
        })


class _FakeAdapter:
    """Minimal adapter conforming to `_ExecAdapter`, with a declared source."""

    execution_source = "docker"

    def __init__(self, result: SandboxResult) -> None:
        self.result = result

    async def execute(
        self, command: str, *, timeout_s: float, cwd: str,
        env_whitelist: Dict[str, str], session_id: Optional[str] = None,
    ) -> SandboxResult:
        return self.result


class _RaisingAdapter:
    """Adapter whose `execute` always faults — exercises the error path."""

    execution_source = "native_host"

    async def execute(
        self, command: str, *, timeout_s: float, cwd: str,
        env_whitelist: Dict[str, str], session_id: Optional[str] = None,
    ) -> SandboxResult:
        raise RuntimeError("boom: infrastructure fault")


class _NoSourceAdapter:
    """Conforms only to the narrow `_ExecAdapter` protocol — no `execution_source`."""

    def __init__(self, result: SandboxResult) -> None:
        self.result = result

    async def execute(
        self, command: str, *, timeout_s: float, cwd: str,
        env_whitelist: Dict[str, str], session_id: Optional[str] = None,
    ) -> SandboxResult:
        return self.result


# --------------------------------------------------------------------------- #
# marker precedes detail, sharing one ref
# --------------------------------------------------------------------------- #


async def test_marker_then_detail_share_one_ref() -> None:
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    try:
        adapter = _FakeAdapter(_res(0, stdout="ok"))
        result = await xl.record_execution(
            adapter, "pytest -q", timeout_s=30.0, cwd="/w",
            env_whitelist={}, session_id="s1", source="run_command",
        )
    finally:
        reset_activity_sink(token)

    assert result.exit_code == 0
    assert [c["op"] for c in sink.calls] == ["marker", "detail"]
    marker, detail = sink.calls
    assert marker["target"] == "pytest -q"
    assert detail["ref"] == marker["ref"]
    assert detail["source"] == "docker"
    assert detail["exit_code"] == 0
    assert detail["error"] is None
    assert detail["stdout"] == "ok"


# --------------------------------------------------------------------------- #
# masking — the sink never sees a raw secret
# --------------------------------------------------------------------------- #


async def test_detail_stdout_is_masked() -> None:
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    try:
        adapter = _FakeAdapter(_res(0, stdout="api_key=HUNTER2SECRET"))
        await xl.record_execution(
            adapter, "echo $KEY", timeout_s=30.0, cwd="/w",
            env_whitelist={}, session_id="s1", source="run_command",
        )
    finally:
        reset_activity_sink(token)

    detail = next(c for c in sink.calls if c["op"] == "detail")
    assert "HUNTER2SECRET" not in (detail["stdout"] or "")
    assert "***REDACTED***" in (detail["stdout"] or "")


# --------------------------------------------------------------------------- #
# truncation — over-cap output arrives flagged
# --------------------------------------------------------------------------- #


async def test_over_cap_output_is_truncated_and_flagged() -> None:
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    try:
        adapter = _FakeAdapter(_res(0, stdout="A" * 5_000))
        await xl.record_execution(
            adapter, "gen", timeout_s=30.0, cwd="/w",
            env_whitelist={}, session_id="s1", source="run_command",
        )
    finally:
        reset_activity_sink(token)

    detail = next(c for c in sink.calls if c["op"] == "detail")
    assert detail["truncated"] is True
    assert len(detail["stdout"] or "") < 5_000


# --------------------------------------------------------------------------- #
# execution_source — read off the adapter, defaulting safely when absent
# --------------------------------------------------------------------------- #


async def test_execution_source_defaults_to_unknown_for_bare_adapter() -> None:
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    try:
        adapter = _NoSourceAdapter(_res(0))
        await xl.record_execution(
            adapter, "echo hi", timeout_s=30.0, cwd="/w",
            env_whitelist={}, session_id="s1", source="run_command",
        )
    finally:
        reset_activity_sink(token)

    detail = next(c for c in sink.calls if c["op"] == "detail")
    assert detail["source"] == "unknown"


# --------------------------------------------------------------------------- #
# unset sink — no turn bound (dev-palette smoke path, DEBT-122)
# --------------------------------------------------------------------------- #


async def test_unset_sink_emits_nothing_and_does_not_raise() -> None:
    assert current_activity_sink() is None  # no bind in this test
    adapter = _FakeAdapter(_res(0))
    result = await xl.record_execution(
        adapter, "echo hi", timeout_s=30.0, cwd="/w",
        env_whitelist={}, session_id=None, source="run_command",
    )
    assert result.exit_code == 0  # ordinary pass-through, unaffected


# --------------------------------------------------------------------------- #
# fault path — execute() raises: a terminal detail still fires, then re-raises
# --------------------------------------------------------------------------- #


async def test_raising_execute_emits_terminal_detail_and_reraises() -> None:
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    try:
        adapter = _RaisingAdapter()
        with pytest.raises(RuntimeError, match="boom"):
            await xl.record_execution(
                adapter, "explode", timeout_s=30.0, cwd="/w",
                env_whitelist={}, session_id="s1", source="run_command",
            )
    finally:
        reset_activity_sink(token)

    assert [c["op"] for c in sink.calls] == ["marker", "detail"]
    detail = sink.calls[1]
    assert detail["exit_code"] is None
    assert detail["error"] is not None
    assert "boom" in detail["error"]
    assert detail["source"] == "native_host"
    # A faulted execution never reaches record_exec, so nothing is logged to
    # the dashboard ring for it — only the timeline sink hears about it.
    assert xl.recent_exec_log()["latest_seq"] == 0


# --------------------------------------------------------------------------- #
# a raising sink never corrupts record_execution's return value
# --------------------------------------------------------------------------- #


class _RaisingSink:
    async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
        raise RuntimeError("sink marker boom")

    async def emit_blocked(self, *, target: str) -> None:
        raise RuntimeError("sink blocked boom")

    async def emit_detail(self, **_kwargs: Any) -> None:
        raise RuntimeError("sink detail boom")


async def test_raising_sink_does_not_corrupt_the_return_value() -> None:
    token = bind_activity_sink(_RaisingSink())
    try:
        adapter = _FakeAdapter(_res(3, stdout="partial"))
        result = await xl.record_execution(
            adapter, "flaky", timeout_s=30.0, cwd="/w",
            env_whitelist={}, session_id="s1", source="run_command",
        )
    finally:
        reset_activity_sink(token)

    # The real outcome survives a broken sink untouched.
    assert result.exit_code == 3
    assert result.stdout == "partial"
    # ...and the dashboard ring still got its entry — sink failure is isolated.
    assert xl.recent_exec_log()["latest_seq"] == 1


# --------------------------------------------------------------------------- #
# the ContextVar is reset after the turn — no cross-turn leakage
# --------------------------------------------------------------------------- #


async def test_sink_is_scoped_and_resets_cleanly() -> None:
    assert current_activity_sink() is None
    sink = _RecordingSink()
    token = bind_activity_sink(sink)
    assert current_activity_sink() is sink
    reset_activity_sink(token)
    assert current_activity_sink() is None


def test_sink_does_not_leak_across_concurrent_tasks() -> None:
    """Two turns bind different sinks; neither sees the other's, and each
    reset only tears down its own binding — the propagation guarantee a
    contextvars.ContextVar gives across asyncio.create_task boundaries."""

    async def _one_turn(marker: str) -> List[str]:
        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            adapter = _FakeAdapter(_res(0, stdout=marker))
            await xl.record_execution(
                adapter, f"echo {marker}", timeout_s=30.0, cwd="/w",
                env_whitelist={}, session_id=marker, source="run_command",
            )
        finally:
            reset_activity_sink(token)
        detail = next(c for c in sink.calls if c["op"] == "detail")
        return [detail["stdout"] or ""]

    async def _go() -> None:
        results = await asyncio.gather(_one_turn("alpha"), _one_turn("beta"))
        assert results == [["alpha"], ["beta"]]

    asyncio.run(_go())
