"""Tests for the per-exec command log (bounded in-memory ring).

Covers the shared secret masker's extraction into ``core.redaction`` (parity +
that telemetry now aliases it), the record/read roundtrip with the ``seq``
cursor, ring eviction, truncation and masking of command + output, and that the
``record_execution`` wrapper returns the adapter result untouched while
appending exactly one entry (hermetic — no real sandbox).
"""
import asyncio
from typing import Dict, Iterator, List, Optional, cast

import pytest

import core.exec_log as xl
import core.telemetry as tele
from core.redaction import mask_secrets
from core.sandbox import SandboxResult


@pytest.fixture(autouse=True)
def _clean_ring() -> Iterator[None]:
    xl._reset_for_tests()
    yield
    xl._reset_for_tests()


def _res(exit_code: int = 0, stdout: str = "", stderr: str = "") -> SandboxResult:
    return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _entries(page: Dict[str, object]) -> List[Dict[str, object]]:
    """Typed view of the heterogeneous page dict for the assertions below."""
    return cast(List[Dict[str, object]], page["entries"])


# ── masker extraction parity ─────────────────────────────────────────────────

def test_masker_shared_and_redacts() -> None:
    # telemetry now aliases the shared masker — same callable, no divergence.
    assert tele._mask_sensitive is mask_secrets
    assert "***REDACTED***" in (mask_secrets("api_key=SUPERSECRETVALUE") or "")
    assert "sk-ABCDEFGH12345678" not in (mask_secrets("key sk-ABCDEFGH12345678") or "")
    assert mask_secrets("") == ""
    assert mask_secrets(None) is None


# ── record / read roundtrip ──────────────────────────────────────────────────

def test_record_roundtrip_and_source() -> None:
    xl.record_exec("run_command", "sess-1", "echo hi", _res(0, "hi\n"), 12.5)
    out = xl.recent_exec_log()
    assert out["latest_seq"] == 1
    entries = _entries(out)
    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "run_command"
    assert e["session_id"] == "sess-1"
    assert e["command"] == "echo hi"
    assert e["exit_code"] == 0
    assert e["output"] == "hi\n"
    assert e["duration_ms"] == 12.5
    assert e["seq"] == 1


def test_empty_ring_zero_seq() -> None:
    out = xl.recent_exec_log()
    assert _entries(out) == []
    assert out["latest_seq"] == 0


# ── seq cursor (idle-poll cheapness) ─────────────────────────────────────────

def test_seq_cursor_returns_only_newer() -> None:
    for i in range(3):
        xl.record_exec("hook", "", f"cmd{i}", _res(0), 1.0)
    assert xl.recent_exec_log()["latest_seq"] == 3

    newer = xl.recent_exec_log(since=1)
    assert [e["seq"] for e in _entries(newer)] == [2, 3]  # chronological, seq>1
    assert newer["latest_seq"] == 3

    idle = xl.recent_exec_log(since=3)
    assert _entries(idle) == []          # nothing newer ⇒ ultralight
    assert idle["latest_seq"] == 3


def test_tail_limits_first_load() -> None:
    for i in range(5):
        xl.record_exec("coder_exec", "", f"cmd{i}", _res(0), 1.0)
    entries = _entries(xl.recent_exec_log(tail=2))
    assert [e["seq"] for e in entries] == [4, 5]  # most-recent tail, chronological


def test_tail_clamped_high() -> None:
    xl.record_exec("type_check", "", "mypy", _res(0), 1.0)
    # An absurd tail must not raise; it is clamped to the ring cap.
    assert len(_entries(xl.recent_exec_log(tail=10_000))) == 1


# ── ring eviction ────────────────────────────────────────────────────────────

def test_ring_evicts_at_capacity() -> None:
    total = xl._RING_CAP + 50
    for i in range(total):
        xl.record_exec("run_command", "", f"c{i}", _res(0), 1.0)
    out = xl.recent_exec_log(tail=xl._RING_CAP)
    assert out["latest_seq"] == total
    entries = _entries(out)
    assert len(entries) == xl._RING_CAP
    # oldest surviving seq is the first not evicted
    assert entries[0]["seq"] == total - xl._RING_CAP + 1


# ── masking + truncation ─────────────────────────────────────────────────────

def test_command_and_output_masked() -> None:
    xl.record_exec(
        "run_command", "",
        "export TOKEN=sk-SECRETVALUE123 && run",
        _res(0, stdout="api_key=HUNTER2SECRET"),
        1.0,
    )
    e = _entries(xl.recent_exec_log())[0]
    command = cast(str, e["command"])
    output = cast(str, e["output"])
    assert "sk-SECRETVALUE123" not in command
    assert "***REDACTED***" in command
    assert "HUNTER2SECRET" not in output
    assert "***REDACTED***" in output


def test_long_output_truncated() -> None:
    big = "A" * 5_000
    xl.record_exec("run_command", "", "gen", _res(0, stdout=big), 1.0)
    out = cast(str, _entries(xl.recent_exec_log())[0]["output"])
    assert len(out) < 5_000
    assert "truncated" in out


# ── record_execution wrapper (hermetic) ──────────────────────────────────────

class _FakeAdapter:
    """Minimal adapter: records the kwargs it saw and returns a fixed result."""

    def __init__(self) -> None:
        self.seen: Dict[str, object] = {}
        self.result = _res(7, stdout="done")

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        self.seen = {"command": command, "timeout_s": timeout_s, "cwd": cwd,
                     "session_id": session_id}
        return self.result


def test_record_execution_passthrough_and_records() -> None:
    adapter = _FakeAdapter()

    async def _go() -> SandboxResult:
        return await xl.record_execution(
            adapter, "pytest -q",
            timeout_s=30.0, cwd="/w", env_whitelist={}, session_id="s9",
            source="coder_verify",
        )

    result = asyncio.run(_go())

    # the wrapper returns the adapter's result object untouched
    assert result is adapter.result
    assert result.exit_code == 7
    assert adapter.seen["session_id"] == "s9"

    # ...and appended exactly one entry, tagged and timed
    entries = _entries(xl.recent_exec_log())
    assert len(entries) == 1
    assert entries[0]["source"] == "coder_verify"
    assert entries[0]["session_id"] == "s9"
    assert entries[0]["command"] == "pytest -q"
    assert isinstance(entries[0]["duration_ms"], float)
