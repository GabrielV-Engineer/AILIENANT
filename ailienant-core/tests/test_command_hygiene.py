# ailienant-core/tests/test_command_hygiene.py
#
# 13.0.9 W1 — validate_step_command (a new pure hygiene filter for a WBS
# run_command step's overloaded target_file field) and the run_guarded_command/
# render_guarded_command_result extraction from SandboxBashTool._arun. The
# extraction's own behavior-preservation is already covered end-to-end by the
# pre-existing test_execute_tier_gate.py / test_execution_tools.py suites
# (which drive it through _arun unchanged); this file exercises the two new
# primitives directly, since agents/coder.py's apply-gate rewrite (13.0.9 W2)
# calls them without going through SandboxBashTool at all.

from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.sandbox import SandboxResult
from tools.execution_tools import (
    _GUARD_REFUSED_EXIT_CODE,
    render_guarded_command_result,
    run_guarded_command,
    validate_step_command,
)
import tools.execution_tools as exec_mod

pytestmark = pytest.mark.anyio


# ─── validate_step_command ────────────────────────────────────────────────────


def test_validate_step_command_accepts_a_real_command() -> None:
    cmd, err = validate_step_command("npm run build")
    assert cmd == "npm run build"
    assert err is None


def test_validate_step_command_rejects_none() -> None:
    cmd, err = validate_step_command(None)
    assert cmd is None
    assert err is not None


@pytest.mark.parametrize("raw", ["", "   "])
def test_validate_step_command_rejects_empty(raw: str) -> None:
    cmd, err = validate_step_command(raw)
    assert cmd is None
    assert "empty" in (err or "")


@pytest.mark.parametrize(
    "raw", ["N/A", "n/a", "NA", "TBD", "tbd", "todo", "None", "-", "--", "...", "<command>"],
)
def test_validate_step_command_rejects_placeholder_tokens(raw: str) -> None:
    """The live-reproduced bug: a planner LLM with nothing concrete to run wrote
    "N/A" into the overloaded target_file field, and it reached a real shell."""
    cmd, err = validate_step_command(raw)
    assert cmd is None
    assert "placeholder" in (err or "")


def test_validate_step_command_rejects_a_bare_source_file_path() -> None:
    """target_file's other meaning ("path of the affected file") leaking through
    on a run_command step must be refused, not executed."""
    cmd, err = validate_step_command("src/components/Layout.jsx")
    assert cmd is None
    assert "file path" in (err or "")


@pytest.mark.parametrize("raw", ["./build.sh", "./gradlew", "pytest", "npm", "make"])
def test_validate_step_command_accepts_real_script_and_bare_executable_invocations(
    raw: str,
) -> None:
    """The path-shaped-string heuristic must not false-positive on a legitimate
    single-token script invocation or bare executable name — only a denylist of
    non-executable source/config extensions trips it, never a bare '/'."""
    cmd, err = validate_step_command(raw)
    assert cmd == raw
    assert err is None


def test_validate_step_command_rejects_over_length_ceiling() -> None:
    cmd, err = validate_step_command("echo " + ("x" * 5000))
    assert cmd is None
    assert "exceeds" in (err or "")


def test_validate_step_command_strips_surrounding_whitespace() -> None:
    cmd, err = validate_step_command("  pytest -q  ")
    assert cmd == "pytest -q"
    assert err is None


# ─── run_guarded_command / render_guarded_command_result ─────────────────────


def _no_adapter(**_: Any) -> Any:
    raise AssertionError("resolve_execution_adapter must not be called when the gate blocks")


async def test_run_guarded_command_deny_never_reaches_an_adapter(monkeypatch: Any) -> None:
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", _no_adapter)
    result = await run_guarded_command(
        "ls", session_id="s1", session_permission_mode="PLAN",
    )
    assert result.exit_code == _GUARD_REFUSED_EXIT_CODE
    assert "DENIED" in render_guarded_command_result(result)


async def test_run_guarded_command_hitl_with_no_session_refuses_without_spawn(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", _no_adapter)
    result = await run_guarded_command(
        "ls", session_id=None, session_permission_mode="DEFAULT",
    )
    assert result.exit_code == _GUARD_REFUSED_EXIT_CODE
    assert "BLOCKED" in render_guarded_command_result(result)


async def test_run_guarded_command_hitl_rejected_never_reaches_an_adapter(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", _no_adapter)
    approve = AsyncMock(return_value={"approved": False})
    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval", approve,
    )
    result = await run_guarded_command(
        "ls", session_id="s1", session_permission_mode="DEFAULT",
    )
    approve.assert_awaited_once()
    assert result.exit_code == _GUARD_REFUSED_EXIT_CODE
    assert "BLOCKED" in render_guarded_command_result(result)


async def test_run_guarded_command_dangerous_pattern_never_reaches_an_adapter(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", _no_adapter)
    result = await run_guarded_command("sudo rm -rf /")
    assert result.exit_code == _GUARD_REFUSED_EXIT_CODE
    assert "DANGEROUS_COMMAND_INTERCEPTED" in render_guarded_command_result(result)


async def test_run_guarded_command_approved_reaches_the_adapter_and_renders_exit_envelope(
    monkeypatch: Any,
) -> None:
    adapter = AsyncMock()
    adapter.execute = AsyncMock(
        return_value=SandboxResult(exit_code=0, stdout="hello\n", stderr="")
    )
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", lambda **_kw: adapter)
    approve = AsyncMock(return_value={"approved": True})
    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval", approve,
    )

    result = await run_guarded_command(
        "echo hello", session_id="s1", session_permission_mode="DEFAULT",
    )
    adapter.execute.assert_awaited_once()
    assert result.exit_code == 0
    assert render_guarded_command_result(result) == "[sandbox_bash] exit=0\nhello\n"


async def test_run_guarded_command_auto_mode_skips_hitl_and_reaches_the_adapter(
    monkeypatch: Any,
) -> None:
    """AUTO -> ALLOW: no approval round trip at all."""
    adapter = AsyncMock()
    adapter.execute = AsyncMock(
        return_value=SandboxResult(exit_code=0, stdout="ok", stderr="")
    )
    monkeypatch.setattr(exec_mod, "resolve_execution_adapter", lambda **_kw: adapter)

    result = await run_guarded_command(
        "echo ok", session_id="s1", session_permission_mode="AUTO",
    )
    adapter.execute.assert_awaited_once()
    assert result.exit_code == 0


def test_render_guarded_command_result_truncates_a_real_execution_body() -> None:
    long_body = "x" * 3000
    result = SandboxResult(exit_code=1, stdout=long_body, stderr="")
    rendered = render_guarded_command_result(result)
    assert rendered.startswith("[sandbox_bash] exit=1\n")
    assert len(rendered) < len(long_body) + 50  # middle-truncated, not passed through raw
