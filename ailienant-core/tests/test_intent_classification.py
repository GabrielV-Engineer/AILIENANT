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

from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskService

pytestmark = pytest.mark.anyio


@pytest.fixture
def service() -> TaskService:
    return TaskService()


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
    with patch("core.task_service.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
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
    with patch("core.task_service.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = RuntimeError("provider down")
        intent = await service._classify_intent(prompt)

    assert intent == "question"


async def test_empty_prompt_routes_to_question(service: TaskService) -> None:
    assert await service._classify_intent("   ") == "question"
