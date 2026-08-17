# ailienant-core/tests/test_ambiguity_gate.py
"""Unit coverage for the two pure/leaf pieces behind the researcher's ambiguity
pre-flight gate and its Plan-mode suggestion:

    is_underspecified          — core/memory/context_auditor.py
    request_graph_clarification — core/hitl.py

The wiring into run_researcher_node (the actual pause/resume + prompt
enrichment behavior) is covered in tests/test_phase4_researcher.py, mirroring
how drift_monitor splits _plan_similarity's pure-function tests from
run_drift_gate_node's node-level tests in tests/test_drift_monitor.py.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from core.hitl import request_graph_clarification
from core.memory.context_auditor import is_underspecified

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# is_underspecified
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_input",
    [
        "fix this",
        "update this",
        "review this please",
        "refactor that",
    ],
)
def test_underspecified_vague_imperatives_flagged(user_input: str) -> None:
    assert is_underspecified(user_input, explicit_mentions=[], active_file_path="") is True


@pytest.mark.parametrize(
    "user_input",
    [
        "fix the login bug in auth.py",  # code signal (path) → concrete anchor
        "how does the login flow work?",  # no action verb
        "refactor the UserRepository class to remove duplication",  # no deictic
        "",  # empty
    ],
)
def test_underspecified_concrete_or_non_actionable_not_flagged(user_input: str) -> None:
    assert is_underspecified(user_input, explicit_mentions=[], active_file_path="") is False


def test_underspecified_disqualified_by_explicit_mention() -> None:
    assert (
        is_underspecified("fix this", explicit_mentions=["app/auth.py"], active_file_path="")
        is False
    )


def test_underspecified_disqualified_by_active_file() -> None:
    assert (
        is_underspecified("fix this", explicit_mentions=[], active_file_path="app/auth.py")
        is False
    )


def test_underspecified_disqualified_by_length() -> None:
    long_prompt = "fix this " + ("and also handle the edge case " * 5)
    assert is_underspecified(long_prompt, explicit_mentions=[], active_file_path="") is False


# ---------------------------------------------------------------------------
# request_graph_clarification — payload shape + resume normalization
# ---------------------------------------------------------------------------


def test_request_graph_clarification_builds_expected_payload() -> None:
    captured: dict[str, Any] = {}

    def _fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {"answer": None, "selected_option": "auth.py"}

    with patch("core.hitl.interrupt", side_effect=_fake_interrupt):
        result = request_graph_clarification(
            session_id="sess-1",
            question="Which file?",
            context="need a concrete target",
            suggested_options=["auth.py", "main.py"],
        )

    payload = captured["payload"]
    assert payload["session_id"] == "sess-1"
    assert payload["request_kind"] == "CLARIFICATION_NEEDED"
    assert payload["question"] == "Which file?"
    assert payload["context"] == "need a concrete target"
    assert payload["suggested_options"] == ["auth.py", "main.py"]
    assert result == {"answer": None, "selected_option": "auth.py"}


def test_request_graph_clarification_defaults_options_to_empty_list() -> None:
    captured: dict[str, Any] = {}

    def _fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {}

    with patch("core.hitl.interrupt", side_effect=_fake_interrupt):
        request_graph_clarification(session_id="sess-1", question="q?")

    assert captured["payload"]["suggested_options"] == []


def test_request_graph_clarification_normalizes_bare_string_resume() -> None:
    with patch("core.hitl.interrupt", return_value="auth.py"):
        result = request_graph_clarification(session_id="s", question="q?")
    assert result == {"answer": "auth.py", "selected_option": None}


def test_request_graph_clarification_normalizes_none_resume() -> None:
    with patch("core.hitl.interrupt", return_value=None):
        result = request_graph_clarification(session_id="s", question="q?")
    assert result == {"answer": None, "selected_option": None}
