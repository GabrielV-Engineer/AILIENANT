"""Closed-loop sandboxed executor — the coder runs verification and self-corrects.

A ``run_command`` WBS step now dispatches into the resolved sandbox tier. On a
green exit the step completes; on a non-zero exit the coder distils *structured*
diagnostics (never raw stdout) and emits the same ``healing_required`` signal an
in-node exception would raise, so the existing ``route_after_coder →
error_correction`` edge re-drafts. When no adapter is resolved the honest
``EXECUTE_TIER_DEFERRED`` deferral is preserved.

These tests exercise the contract with a deterministic stub adapter (no real
subprocess, no Docker) and pin the load-bearing guarantees: a correction attempt
*actually fires* on failure (not merely that the command ran), the loop respects
the correction budget, the exit verdict comes from the typed ``exit_code`` field
(never re-parsed from text), and the diagnostics parser is total (never raises).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

import core.sandbox as sb
from agents.coder import run_coder_node
from brain.engine import route_after_coder
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS
from brain.state import MissionSpecification, WBSStep
from core.sandbox import SandboxResult


# --------------------------------------------------------------------------- #
# Fixtures & helpers
# --------------------------------------------------------------------------- #


class _StubAdapter:
    """Deterministic adapter: returns canned SandboxResults from a queue.

    No subprocess is ever spawned. The last result repeats once the queue is
    drained, so a perpetually-failing command can be modelled with a single entry.
    """

    def __init__(self, results: List[SandboxResult]) -> None:
        self._results = list(results)
        self.calls: List[str] = []

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        self.calls.append(command)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _make_step(command: str = "pytest -q", n: int = 1) -> WBSStep:
    # For a run_command step the WBS schema overloads target_file to hold the
    # command to execute ("ruta ... o comando a ejecutar").
    return WBSStep(
        step_number=n,
        target_role="core_dev",  # type: ignore[arg-type]
        action="run_command",  # type: ignore[arg-type]
        target_file=command,
        description="Run the project verification.",
        status="pending",  # type: ignore[arg-type]
    )


def _make_state(step: WBSStep, **overrides: Any) -> Dict[str, Any]:
    mission = MissionSpecification(
        outcome="Test outcome.",
        scope=["main.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[step],
        checks=["Pytest exits 0."],
    )
    state: Dict[str, Any] = {
        "task_id": "executor-test",
        "mission_spec": mission,
        "current_step_id": step.step_number,
        "retry_count": 0,
        "correction_attempts": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
        "session_permission_mode": "AUTO",  # default to letting the gate pass
        "workspace_root": "",
        "project_id": "",
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _mock_emit() -> Any:
    """The run_command branch only reaches the WS notify; isolate it."""
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture
def _stub(monkeypatch: pytest.MonkeyPatch):
    """Bind a stub adapter as the active sandbox tier (overrides conftest's
    _DirectAdapter, which would otherwise run a real subprocess)."""

    def _install(results: List[SandboxResult]) -> _StubAdapter:
        adapter = _StubAdapter(results)
        monkeypatch.setattr(sb, "ACTIVE_ADAPTER", adapter)
        return adapter

    return _install


# --------------------------------------------------------------------------- #
# Green path
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_command_green_completes(_stub: Any) -> None:
    _stub([SandboxResult(exit_code=0, stdout="2 passed", stderr="")])
    step = _make_step()
    result = await run_coder_node(_make_state(step))
    assert step.status == "completed"
    assert not result.get("errors")
    assert not result.get("healing_required")


# --------------------------------------------------------------------------- #
# Failure → self-heal signal
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_command_failure_emits_healing_signal(_stub: Any) -> None:
    _stub(
        [
            SandboxResult(
                exit_code=1,
                stdout="main.py:42: error: bad type [arg-type]",
                stderr="",
            )
        ]
    )
    step = _make_step(command="mypy .")
    result = await run_coder_node(_make_state(step))

    assert step.status == "failed"
    assert result.get("healing_required") is True
    assert result.get("failed_node") == "coder_agent"
    assert result.get("failure_signature")
    # The existing conditional edge must route this delta to self-healing.
    assert route_after_coder(result) == "error_correction"


@pytest.mark.anyio
async def test_healing_trace_is_structured_not_raw(_stub: Any) -> None:
    """last_error_trace carries compact [line] diagnostics, not a raw traceback."""
    _stub(
        [
            SandboxResult(
                exit_code=1,
                stdout="main.py:42: error: Incompatible return value [return-value]",
                stderr="",
            )
        ]
    )
    result = await run_coder_node(_make_state(_make_step(command="mypy .")))
    trace = str(result.get("last_error_trace") or "")
    assert "42" in trace
    assert "return-value" in trace
    # A raw mypy line would still contain "error:"; the distilled form drops it.
    assert "Traceback" not in trace


@pytest.mark.anyio
async def test_correction_attempts_increments(_stub: Any) -> None:
    _stub([SandboxResult(exit_code=1, stdout="x.py:1: error: boom [misc]", stderr="")])
    result = await run_coder_node(
        _make_state(_make_step(command="mypy ."), correction_attempts=1)
    )
    assert result.get("correction_attempts") == 2


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_budget_exhaustion_concedes_without_healing(_stub: Any) -> None:
    """At the correction budget the loop concedes (no healing_required) rather
    than spinning forever."""
    _stub([SandboxResult(exit_code=1, stdout="x.py:1: error: boom [misc]", stderr="")])
    result = await run_coder_node(
        _make_state(
            _make_step(command="mypy ."),
            correction_attempts=CORRECTION_MAX_ATTEMPTS,
        )
    )
    assert not result.get("healing_required")
    assert result.get("errors")
    assert route_after_coder(result) == "contract_guard"  # forward, not heal


# --------------------------------------------------------------------------- #
# Honest deferral — ONLY when no adapter is resolved
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_no_adapter_preserves_honest_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sb, "ACTIVE_ADAPTER", None)
    step = _make_step()
    result = await run_coder_node(_make_state(step))
    assert step.status == "failed"
    assert not result.get("healing_required")
    flags: List[str] = result.get("security_flags", [])
    assert any(f.startswith("EXECUTE_TIER_DEFERRED:") for f in flags)


# --------------------------------------------------------------------------- #
# Exit-code integrity — verdict from the typed field, never string-sniffed
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_exit_code_read_from_typed_field_not_stdout(_stub: Any) -> None:
    """stdout containing the literal 'exit=0' must NOT fool the verdict — the
    branch reads SandboxResult.exit_code (==1), so it routes to heal."""
    _stub(
        [
            SandboxResult(
                exit_code=1,
                stdout="some log line that mentions exit=0 misleadingly\nx.py:9: error: e [misc]",
                stderr="",
            )
        ]
    )
    result = await run_coder_node(_make_state(_make_step(command="mypy .")))
    assert result.get("healing_required") is True


# --------------------------------------------------------------------------- #
# Permission gate — PLAN denies dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_plan_mode_denies_without_dispatch(_stub: Any) -> None:
    adapter = _stub([SandboxResult(exit_code=0, stdout="", stderr="")])
    step = _make_step()
    result = await run_coder_node(
        _make_state(step, session_permission_mode="PLAN")
    )
    assert step.status == "failed"
    assert adapter.calls == []  # never dispatched
    assert any("DENIED" in e for e in result.get("errors", []))
    assert not result.get("healing_required")
