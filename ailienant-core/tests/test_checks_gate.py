# ailienant-core/tests/test_checks_gate.py
"""brain/checks_gate.py — executes the plan's own acceptance criteria (13.1.3, §8.7).

Project-wide, `MissionSpecification.checks` had exactly one consumer before this:
a logger counting them. This module is the answer — a terminal graph node that
actually runs the mechanically executable subset, through the same guarded-
command path every other execution goes through, and reports everything else
honestly as unverified rather than silently passing.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from brain.checks_gate import match_executable_command, run_checks_node

pytestmark = pytest.mark.anyio


# ─── match_executable_command — pure, no I/O ───────────────────────────────


def test_matches_pytest() -> None:
    assert match_executable_command("Pytest exits 0.") == "pytest"


def test_matches_mypy() -> None:
    assert match_executable_command("mypy passes with no errors") == "mypy ."


def test_matches_ruff() -> None:
    assert match_executable_command("ruff check has zero violations") == "ruff check ."


def test_matches_npm_test() -> None:
    assert match_executable_command("npm test succeeds") == "npm test"
    assert match_executable_command("npm run test passes") == "npm test"


def test_matches_npm_build() -> None:
    assert match_executable_command("npm run build completes without errors") == "npm run build"


def test_matches_npm_lint() -> None:
    assert match_executable_command("npm run lint has no errors") == "npm run lint"
    assert match_executable_command("eslint reports no issues") == "npm run lint"


def test_semantic_check_does_not_match_anything() -> None:
    """The exact live-incident shape: a check requiring code review, not a
    shell command, must be reported unverified rather than force-matched to
    something unrelated."""
    check = (
        "Verify that FeatureCard and Testimonial are correctly utilized by "
        "their parent containers via props."
    )
    assert match_executable_command(check) is None


def test_empty_check_does_not_match() -> None:
    assert match_executable_command("") is None


# ─── run_checks_node — the graph-facing node ───────────────────────────────


def _mission(checks: list[str]) -> Any:
    return SimpleNamespace(checks=checks)


def _state(checks: list[str], **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "task_id": "sess-1",
        "mission_spec": _mission(checks),
        "workspace_root": "/ws",
        "session_permission_mode": "STANDARD",
    }
    base.update(overrides)
    return base


async def test_no_op_when_the_plan_has_no_checks() -> None:
    result = await run_checks_node(_state([]))
    assert result == {}


async def test_unexecutable_check_is_reported_unverified_never_dropped() -> None:
    state = _state(["Verify FeatureCard receives props correctly."])
    result = await run_checks_node(state)
    assert result["check_results"] == [
        {"check": "Verify FeatureCard receives props correctly.", "command": None, "status": "unverified"},
    ]
    assert "errors" not in result


async def test_passing_check_runs_the_matched_command_and_reports_passed() -> None:
    from core.sandbox import SandboxResult

    state = _state(["Pytest exits 0."])
    mock_run = AsyncMock(return_value=SandboxResult(exit_code=0, stdout="3 passed", stderr=""))
    with patch("tools.execution_tools.run_guarded_command", mock_run):
        result = await run_checks_node(state)

    mock_run.assert_awaited_once()
    assert mock_run.await_args is not None
    assert mock_run.await_args.args[0] == "pytest"
    assert mock_run.await_args.kwargs["session_permission_mode"] == "STANDARD"
    assert result["check_results"] == [
        {"check": "Pytest exits 0.", "command": "pytest", "status": "passed"},
    ]
    assert "errors" not in result


async def test_failing_check_is_reported_failed_and_folded_into_errors() -> None:
    """This is C2: a failed acceptance check must reach the SAME `errors`
    channel the turn-end summary already reads — a failed check can never be
    silently absorbed into a reported success."""
    from core.sandbox import SandboxResult

    state = _state(["Pytest exits 0."])
    mock_run = AsyncMock(return_value=SandboxResult(exit_code=1, stdout="", stderr="2 failed, 1 passed"))
    with patch("tools.execution_tools.run_guarded_command", mock_run):
        result = await run_checks_node(state)

    assert result["check_results"][0]["status"] == "failed"
    assert "errors" in result and result["errors"]
    assert "Pytest exits 0." in result["errors"][0]


async def test_a_denied_command_degrades_to_unverified_not_a_failure() -> None:
    """A PLAN_ONLY session (or any permission mode that denies execution) must
    not let a check's guard-refusal masquerade as either a pass OR a failure
    of the check's own condition — a refusal to attempt says nothing about
    whether the underlying condition holds, so it is honestly 'unverified',
    the same as a check with no recognised command at all."""
    from tools.execution_tools import _GUARD_REFUSED_EXIT_CODE
    from core.sandbox import SandboxResult

    state = _state(["Pytest exits 0."], session_permission_mode="PLAN_ONLY")
    mock_run = AsyncMock(
        return_value=SandboxResult(exit_code=_GUARD_REFUSED_EXIT_CODE, stdout="denied", stderr="")
    )
    with patch("tools.execution_tools.run_guarded_command", mock_run):
        result = await run_checks_node(state)

    assert result["check_results"][0]["status"] == "unverified"
    assert "errors" not in result


async def test_multiple_checks_mixed_verdicts() -> None:
    from core.sandbox import SandboxResult

    state = _state(["Pytest exits 0.", "Verify props are wired correctly.", "npm run build succeeds"])
    mock_run = AsyncMock(side_effect=[
        SandboxResult(exit_code=0, stdout="ok", stderr=""),
        SandboxResult(exit_code=1, stdout="", stderr="build error"),
    ])
    with patch("tools.execution_tools.run_guarded_command", mock_run):
        result = await run_checks_node(state)

    statuses = [r["status"] for r in result["check_results"]]
    assert statuses == ["passed", "unverified", "failed"]
    assert len(result["errors"]) == 1


async def test_a_guard_exception_degrades_to_unverified_never_raises() -> None:
    state = _state(["Pytest exits 0."])
    with patch("tools.execution_tools.run_guarded_command", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await run_checks_node(state)  # must not raise

    assert result["check_results"][0]["status"] == "unverified"
    assert "errors" not in result


async def test_no_mission_spec_is_a_safe_no_op() -> None:
    result = await run_checks_node({"task_id": "sess-1"})
    assert result == {}
