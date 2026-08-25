"""The coder's run_command staging, and the apply gate's execution/self-heal.

A ``run_command`` WBS step used to dispatch into the sandbox tier and
self-correct from inside ``run_coder_node`` itself. As of 13.0.9's incremental
apply gate (``brain/apply_gate.py``), the coder only validates the command
string and stages it in ``pending_step_command``; the permission verdict,
HITL approval, actual dispatch, and self-heal on a non-zero exit all moved
downstream into ``run_apply_commit_node``. That relocated behavior's general
shape (success, healing-within-budget, budget exhaustion, deny) is covered by
``tests/test_task_service_apply.py`` alongside the rest of the apply gate;
this file keeps only the guarantees specific to command execution that file
doesn't otherwise pin: diagnostics are distilled (never raw stdout), the
verdict comes from the typed ``exit_code`` field (never string-sniffed), and
a healing signal emitted by the gate still reaches ``error_correction``
through ``route_after_validation`` (the coder itself no longer executes
anything for this action, so ``route_after_coder`` is no longer the relevant
edge).

The remaining coder-side tests exercise ``run_coder_node``'s narrower
responsibility with a deterministic stub adapter (no real subprocess, no
Docker): the honest ``EXECUTE_TIER_DEFERRED`` deferral when no adapter is
resolved, and refusal of a hygiene-invalid command before it is ever staged.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

import core.sandbox as sb
from agents.coder import run_coder_node
from brain.apply_gate import run_apply_commit_node
from brain.guardrails import route_after_validation
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


def _command_apply_state(command: str = "mypy .") -> Dict[str, Any]:
    """A state that already reached the apply gate with this step's command
    staged — mirrors what run_apply_prepare_node would have committed."""
    step = _make_step(command=command)
    mission = MissionSpecification(
        outcome="Test outcome.", scope=["main.py"], constraints=[], decisions=[],
        tasks=[step], checks=["Pytest exits 0."],
    )
    return {
        "task_id": "executor-test",
        "project_id": "",
        "mission_spec": mission,
        "current_step_id": step.step_number,
        "correction_attempts": 0,
        "applied_step_ids": [],
        "applied_files_log": [],
        "pending_apply": {
            "step_number": step.step_number, "kind": "COMMAND_EXECUTE", "decision": "allow",
            "files": [], "command": command, "risk_labels": [], "auto_accept": False, "attempt": 0,
        },
    }


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
# run_coder_node — staging only, no dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_no_adapter_preserves_honest_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sb, "ACTIVE_ADAPTER", None)
    step = _make_step()
    result = await run_coder_node(_make_state(step))
    assert result["mission_spec"].tasks[0].status == "failed"
    assert not result.get("healing_required")
    flags: List[str] = result.get("security_flags", [])
    assert any(f.startswith("EXECUTE_TIER_DEFERRED:") for f in flags)


@pytest.mark.anyio
async def test_invalid_command_is_refused_before_staging(_stub: Any) -> None:
    """A hygiene-invalid command (the WBS schema's overloaded target_file field
    producing a placeholder like "N/A") must never reach pending_step_command —
    it is refused here, before the apply gate ever sees it."""
    adapter = _stub([SandboxResult(exit_code=0, stdout="", stderr="")])
    step = _make_step(command="N/A")
    result = await run_coder_node(_make_state(step))
    assert result["mission_spec"].tasks[0].status == "failed"
    assert adapter.calls == []  # never dispatched
    assert not result.get("pending_step_command")
    assert any("refused before execution" in e for e in result.get("errors", []))


# --------------------------------------------------------------------------- #
# run_apply_commit_node — execution + self-heal (the part unique to this file)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_healing_trace_is_structured_not_raw() -> None:
    """last_error_trace carries compact [line] diagnostics, not a raw traceback."""
    state = _command_apply_state("mypy .")
    fail_result = SimpleNamespace(
        exit_code=1, stdout="main.py:42: error: Incompatible return value [return-value]", stderr="",
    )
    with patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=fail_result)):
        result = await run_apply_commit_node(state)
    trace = str(result.get("last_error_trace") or "")
    assert "42" in trace
    assert "return-value" in trace
    # A raw mypy line would still contain "error:"; the distilled form drops it.
    assert "Traceback" not in trace


@pytest.mark.anyio
async def test_exit_code_read_from_typed_field_not_stdout() -> None:
    """stdout containing the literal 'exit=0' must NOT fool the verdict — the
    branch reads SandboxResult.exit_code (==1), so it routes to heal."""
    state = _command_apply_state("mypy .")
    fail_result = SimpleNamespace(
        exit_code=1,
        stdout="some log line that mentions exit=0 misleadingly\nx.py:9: error: e [misc]",
        stderr="",
    )
    with patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=fail_result)):
        result = await run_apply_commit_node(state)
    assert result.get("healing_required") is True


@pytest.mark.anyio
async def test_command_healing_signal_routes_through_validate_output() -> None:
    """The self-heal signal now originates in apply_commit, not the coder —
    route_after_coder is no longer in the loop for this action; the edge that
    must carry it is route_after_validation, downstream of validate_output."""
    state = _command_apply_state("mypy .")
    fail_result = SimpleNamespace(exit_code=1, stdout="x.py:1: error: boom [misc]", stderr="")
    with patch("tools.execution_tools.run_guarded_command", new=AsyncMock(return_value=fail_result)):
        result = await run_apply_commit_node(state)
    assert route_after_validation(result) == "error_correction"
