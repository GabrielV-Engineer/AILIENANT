# tests/test_deterministic_gates.py
"""Phase 4.2 DoD — Deterministic Validators (syntax/style gates + environment).

Six tests:
  A.  syntax_gate catches SyntaxError via ast.parse.
  B.  verify_environment falls back to sys.executable when no override.
  C.  Give-Up Gate latches style_bypass_active when consecutive_style_failures
      reaches STYLE_BYPASS_THRESHOLD (= 2).
  D.  syntax_gate passes valid code.
  E.  style_gate resets counter on pass.
  F.  R8/R9 robustness — missing interpreter returns clean fail, not a crash.

All node tests assert the R1 state-key contract: returned keys ⊆ declared
AIlienantGraphState fields.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Set
from unittest.mock import AsyncMock, patch

import pytest


ALLOWED_STATE_KEYS: Set[str] = {
    "syntax_gate_status",
    "consecutive_style_failures",
    "style_bypass_active",
    "errors",
    "security_flags",
    "venv_interpreter_path",
    "relaxed_typing_mode",
}


def _assert_state_key_contract(result: Dict[str, Any]) -> None:
    """R1 — every returned key must be a declared AIlienantGraphState field."""
    extras = set(result.keys()) - ALLOWED_STATE_KEYS
    assert not extras, f"Phantom state keys returned by node: {extras}"


# ── Test A — syntax gate catches malformed code ──────────────────────────────


@pytest.mark.anyio
async def test_syntax_gate_catches_malformed_code() -> None:
    from validators.gates import syntax_gate_node

    state: Dict[str, Any] = {"code_under_validation": "def broken(:\n    pass"}
    result = await syntax_gate_node(state)

    assert result["syntax_gate_status"] == "fail"
    assert "errors" in result and result["errors"]
    assert "SyntaxError" in result["errors"][0]
    _assert_state_key_contract(result)


# ── Test B — environment falls back to sys.executable ────────────────────────


@pytest.mark.anyio
async def test_verify_environment_falls_back_to_sys_executable(tmp_path: Any) -> None:
    from validators.environment import verify_environment_node

    # Point workspace_root at an empty tmp_path so no mypy.ini / pyproject.toml
    # is found — exercises the relaxed_typing_mode=True branch as a bonus.
    state: Dict[str, Any] = {"workspace_root": str(tmp_path)}
    result = await verify_environment_node(state)

    assert result["venv_interpreter_path"] == sys.executable
    assert result["relaxed_typing_mode"] is True  # no typing config in tmp_path
    _assert_state_key_contract(result)


# ── Test C — Give-Up Gate latches style_bypass_active at threshold ───────────


@pytest.mark.anyio
async def test_give_up_gate_latches_style_bypass_active_after_threshold() -> None:
    from validators.gates import style_gate_node

    with patch(
        "validators.gates.validate_style",
        new=AsyncMock(return_value=(False, "E501 line too long")),
    ):
        # Counter starts at 1 (one prior style-only failure on this step);
        # this invocation pushes it to 2 → threshold tripped.
        state: Dict[str, Any] = {
            "code_under_validation": "x = 1",
            "syntax_gate_status": "pass",
            "consecutive_style_failures": 1,
        }
        result = await style_gate_node(state)

    assert result["consecutive_style_failures"] == 2
    assert result["style_bypass_active"] is True
    assert "STYLE_BYPASS_ACTIVATED" in result["security_flags"]
    assert "errors" in result and result["errors"]
    _assert_state_key_contract(result)


# ── Test D — syntax gate passes valid code ───────────────────────────────────


@pytest.mark.anyio
async def test_syntax_gate_passes_valid_code() -> None:
    from validators.gates import syntax_gate_node

    state: Dict[str, Any] = {"code_under_validation": "def foo() -> int:\n    return 42\n"}
    result = await syntax_gate_node(state)

    assert result["syntax_gate_status"] == "pass"
    assert "errors" not in result
    _assert_state_key_contract(result)


# ── Test E — style gate resets counter on pass ───────────────────────────────


@pytest.mark.anyio
async def test_style_gate_resets_counter_on_pass() -> None:
    from validators.gates import style_gate_node

    with patch(
        "validators.gates.validate_style", new=AsyncMock(return_value=(True, None))
    ):
        state: Dict[str, Any] = {
            "code_under_validation": "x = 1",
            "syntax_gate_status": "pass",
            "consecutive_style_failures": 1,
        }
        result = await style_gate_node(state)

    assert result["consecutive_style_failures"] == 0
    assert "style_bypass_active" not in result  # not latched on a pass
    assert "errors" not in result
    _assert_state_key_contract(result)


# ── Test F — R8/R9 robustness: missing interpreter returns clean fail ────────


@pytest.mark.anyio
async def test_validate_style_handles_missing_interpreter_gracefully() -> None:
    from validators.gates import validate_style

    with patch(
        "validators.gates.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError(),
    ):
        passed, err = await validate_style("x = 1", interpreter="/nonexistent/python")

    assert passed is False
    assert err is not None
    assert "interpreter not found" in err
