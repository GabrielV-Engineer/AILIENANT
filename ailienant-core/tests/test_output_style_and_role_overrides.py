"""Answer-style preference + per-role prompt overrides.

Both settings were previously persisted and read back into the command menu but
never consulted at runtime. These tests pin the two behaviours plus the safety
boundary between them: an answer-style directive shapes conversational replies
and must never reach the CoderAgent's prompt, whose SEARCH/REPLACE output
contract a style instruction would fight.
"""
import asyncio
from typing import Any, Dict
from unittest.mock import patch

import pytest

from agents.roles import ROLE_REGISTRY, _BASE_CODER_PROMPT, build_coder_system_prompt
from core import task_service


def _with_style(style: str) -> Any:
    """Patch the settings read that backs the answer-style preference."""
    return patch(
        "api.system_settings._read_settings",
        return_value={"output_style": style, "permission_mode": "default"},
    )


# ── Answer style on the conversational surface ────────────────────────────────


def test_default_style_leaves_the_chat_prompt_untouched() -> None:
    """An unset preference must be byte-identical to having no style feature."""
    with _with_style("default"):
        assert task_service._resolve_chat_system_prompt("what is this?") == (
            task_service._CHAT_SYSTEM_PROMPT
        )


@pytest.mark.parametrize("style", ["concise", "explanatory", "code_only"])
def test_each_style_appends_its_directive(style: str) -> None:
    with _with_style(style):
        prompt = task_service._resolve_chat_system_prompt("what is this?")
    directive = task_service._OUTPUT_STYLE_DIRECTIVES[style]
    assert prompt.endswith(directive)
    # The base prompt is retained in full — the style is additive, not a swap.
    assert prompt.startswith(task_service._CHAT_SYSTEM_PROMPT)


def test_style_is_appended_last_so_it_wins_over_the_expansive_variant() -> None:
    """An explanation request selects the expansive base; an explicit "concise"
    choice must still be the final word."""
    with _with_style("concise"):
        prompt = task_service._resolve_chat_system_prompt("explain this module")
    assert prompt.startswith(task_service._CHAT_SYSTEM_PROMPT_EXPANSIVE)
    assert prompt.endswith(task_service._OUTPUT_STYLE_DIRECTIVES["concise"])


def test_unknown_style_value_degrades_to_no_directive() -> None:
    with _with_style("haiku"):
        assert task_service._resolve_chat_system_prompt("x") == task_service._CHAT_SYSTEM_PROMPT


def test_unreadable_settings_never_break_the_turn() -> None:
    with patch("api.system_settings._read_settings", side_effect=OSError("disk gone")):
        assert task_service._resolve_output_style_directive() == ""


# ── Safety boundary: style must not reach the strict edit contract ────────────


@pytest.mark.parametrize("style", ["concise", "explanatory", "code_only"])
def test_no_style_directive_reaches_the_coder_prompt(style: str) -> None:
    """The coder prompt mandates SEARCH/REPLACE blocks — a machine-parsed
    contract. "Just the code, minimal prose" or "narrate every step" pushes
    directly against it, so answer style is scoped to the chat surface only.
    Guard this rather than trusting a future reader to re-derive it."""
    with _with_style(style):
        prompt = build_coder_system_prompt("core_dev")
    directive = task_service._OUTPUT_STYLE_DIRECTIVES[style]
    assert directive not in prompt
    assert "STYLE:" not in prompt


# ── Per-role directive overrides ──────────────────────────────────────────────


def test_override_replaces_the_role_directive_only() -> None:
    base_directive = ROLE_REGISTRY["core_dev"]["system_prompt"]
    prompt = build_coder_system_prompt("core_dev", override="Prefer dataclasses.")

    assert "Prefer dataclasses." in prompt
    assert base_directive not in prompt
    # The immutable contract survives verbatim — it is never user-replaceable.
    assert prompt.startswith(_BASE_CODER_PROMPT)
    assert "SEARCH/REPLACE" in prompt


@pytest.mark.parametrize("override", [None, "", "   "])
def test_blank_override_reverts_to_the_builtin_directive(override: Any) -> None:
    """Matches the save endpoint's "empty override deletes the row" semantics."""
    assert build_coder_system_prompt("core_dev", override=override) == (
        build_coder_system_prompt("core_dev")
    )


def test_override_for_a_different_role_does_not_leak() -> None:
    overrides = {"devops_infra": "Never touch production."}
    prompt = build_coder_system_prompt(
        "core_dev", override=overrides.get("core_dev")
    )
    assert "Never touch production." not in prompt
    assert ROLE_REGISTRY["core_dev"]["system_prompt"] in prompt


def test_task_start_threads_overrides_into_state() -> None:
    """The catalog read is async and build_coder_system_prompt is sync, so the
    overrides must arrive with the state (the active_skills pattern)."""
    stored = {"core_dev": "House style: no bare excepts."}

    async def _run() -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        with patch("core.db.list_agent_overrides", return_value=stored) as _:
            from core.db import list_agent_overrides
            state["agent_role_overrides"] = await list_agent_overrides()
        return state

    state = asyncio.run(_run())
    prompt = build_coder_system_prompt(
        "core_dev", override=state["agent_role_overrides"].get("core_dev")
    )
    assert "House style: no bare excepts." in prompt
