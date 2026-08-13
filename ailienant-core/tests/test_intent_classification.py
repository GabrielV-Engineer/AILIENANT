# ailienant-core/tests/test_intent_classification.py
"""TaskService._classify_intent — edit vs question heuristic.

Regression coverage for a misroute where a request that explicitly asks for an
explanation ("Analyze the code... explain it... how can we improve it?") was
short-circuited to 'edit' purely because it contained an edit verb ("make")
somewhere in the text, without ever weighing the explanation cues also present.
A misrouted edit is disruptive (unwanted files/diffs, a spurious stale-guard trip
on the next real edit); a misrouted question merely under-delivers — so the
heuristic must never eagerly commit to 'edit' when an explanation signal
co-occurs with an edit verb; it should escalate to the LLM tie-break instead
(which itself safely defaults to 'question' on any failure).
"""
from __future__ import annotations

from typing import Generator
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import (
    TaskService,
    _resolve_chat_system_prompt,
    _CHAT_SYSTEM_PROMPT,
    _CHAT_SYSTEM_PROMPT_EXPANSIVE,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def service() -> TaskService:
    return TaskService()


@pytest.fixture(autouse=True)
def _default_output_style() -> Generator[None, None, None]:
    """_resolve_chat_system_prompt reads the real ~/.ailienant/settings.json for
    the output-style preference (11.13). This file's tests assert exact prompt
    equality and predate that dependency, so they must not be coupled to
    whatever style happens to be saved on the machine running them."""
    with patch(
        "api.system_settings._read_settings",
        return_value={"output_style": "default", "permission_mode": "default"},
    ):
        yield


async def test_pure_edit_request_routes_to_edit(service: TaskService) -> None:
    """No explanation cue present → the fast heuristic path still short-circuits."""
    intent = await service._classify_intent("Add a login button to the navbar.")
    assert intent == "edit"


async def test_pure_question_routes_to_question(service: TaskService) -> None:
    intent = await service._classify_intent("How does the login flow work?")
    assert intent == "question"


async def test_explain_request_with_no_edit_verb_routes_to_question(service: TaskService) -> None:
    intent = await service._classify_intent(
        "Analyze the code and explain the architecture to me."
    )
    assert intent == "question"


async def test_explain_request_with_edit_verb_escalates_to_tie_break_not_edit(
    service: TaskService,
) -> None:
    """The exact reported misroute: an explanation request whose text also contains
    an edit verb ("make") must NOT be eagerly classified as 'edit' — it should
    escalate to the LLM tie-break, which here returns 'question'."""
    prompt = (
        "Analyze all the code you implemented and explain it to me in detail. "
        "Tell me what you did, what decisions you made, etc. How can we improve "
        "it to make it a professional game? Explain everything to me in detail."
    )
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock(message=AsyncMock(content='{"intent": "question"}'))]
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = mock_response
        intent = await service._classify_intent(prompt)

    mock_llm.assert_awaited_once()  # heuristic did NOT short-circuit — it escalated
    assert intent == "question"


async def test_mixed_signal_defaults_to_question_when_llm_call_fails(
    service: TaskService,
) -> None:
    """The tie-break's own safety net: an LLM fault during an ambiguous (edit-verb +
    explain-signal) prompt must degrade to 'question', never silently to 'edit'."""
    prompt = "Explain the plan, then make the change we discussed."
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = RuntimeError("provider down")
        intent = await service._classify_intent(prompt)

    assert intent == "question"


async def test_empty_prompt_routes_to_question(service: TaskService) -> None:
    assert await service._classify_intent("   ") == "question"


# ─── _resolve_chat_system_prompt — adaptive answer depth (Item C) ─────────────
#
# The question route (_stream_chat_answer) passes no max_tokens at all — the
# terse answers reported in the live-test sweep were authored by
# _CHAT_SYSTEM_PROMPT's "directly and concisely"/"briefly" wording, not a
# truncation. Reuses _EXPLAIN_SIGNALS so intent classification and answer depth
# agree on what counts as an explanation request.


def test_short_question_gets_the_concise_prompt() -> None:
    # Deliberately avoids every _EXPLAIN_SIGNALS term (including "how does"/"why
    # does", which do count as explanation signals by design) — a genuinely
    # short factual question with no explanation cue.
    assert _resolve_chat_system_prompt("Is the database connected right now?") == _CHAT_SYSTEM_PROMPT


def test_explain_request_gets_the_expansive_prompt() -> None:
    assert (
        _resolve_chat_system_prompt("Explain the code you wrote in detail.")
        == _CHAT_SYSTEM_PROMPT_EXPANSIVE
    )


def test_explain_signal_detection_is_case_insensitive() -> None:
    assert (
        _resolve_chat_system_prompt("ANALYZE what you built and WALK ME THROUGH it")
        == _CHAT_SYSTEM_PROMPT_EXPANSIVE
    )


def test_expansive_prompt_does_not_say_concisely_or_briefly() -> None:
    """The whole point of the swap: the depth variant must not contradict itself
    by keeping the brevity wording that caused the original bug."""
    text = _CHAT_SYSTEM_PROMPT_EXPANSIVE.lower()
    assert "concisely" not in text
    assert "briefly" not in text
