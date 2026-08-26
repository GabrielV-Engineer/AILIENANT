# tests/test_deterministic_gates.py
"""Environment probe — verify_environment_node (interpreter + typing-config).

The syntax/style gate coverage this file originally also carried
(validators/gates.py) was removed with that module — it had no production
caller; the main graph never dispatched into it. verify_environment_node's
own production caller was the same dead dispatch path, but its output fields
(venv_interpreter_path, relaxed_typing_mode) are still declared on
AIlienantGraphState, so the probe itself is kept live and tested here pending
a decision on whether to wire it into the main graph or retire it too.
"""
from __future__ import annotations

from typing import Any, Dict, Set

import pytest


ALLOWED_STATE_KEYS: Set[str] = {
    "venv_interpreter_path",
    "relaxed_typing_mode",
}


def _assert_state_key_contract(result: Dict[str, Any]) -> None:
    """Every returned key must be a declared AIlienantGraphState field."""
    extras = set(result.keys()) - ALLOWED_STATE_KEYS
    assert not extras, f"Phantom state keys returned by node: {extras}"


@pytest.mark.anyio
async def test_verify_environment_falls_back_to_sys_executable(tmp_path: Any) -> None:
    import sys

    from validators.environment import verify_environment_node

    # Point workspace_root at an empty tmp_path so no mypy.ini / pyproject.toml
    # is found — exercises the relaxed_typing_mode=True branch as a bonus.
    state: Dict[str, Any] = {"workspace_root": str(tmp_path)}
    result = await verify_environment_node(state)

    assert result["venv_interpreter_path"] == sys.executable
    assert result["relaxed_typing_mode"] is True  # no typing config in tmp_path
    _assert_state_key_contract(result)
