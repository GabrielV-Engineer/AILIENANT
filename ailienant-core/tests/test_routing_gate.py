"""13.1.10 — Reviewable Model Route (brain/routing_gate.py).

`run_model_route_node` is deliberately single-phase (unlike `run_synthesis_node`'s
defer-then-interrupt-first split): resolving a routing decision is a pure re-read
of `context_metrics`, already in state, not an LLM call — so replaying the node
from the top on a resume reproduces the exact same value every time, and there is
no hazard in resolving and interrupting within one invocation. These tests certify
that assumption holds (the review function is called at most once per resolve) and
the three real behaviors: AUTO bypasses entirely, ASK/PLAN suspend, and a cancelled
review clears the confirmed decision instead of leaving a stale one.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest
from langchain_core.runnables import RunnableConfig

from brain.routing_gate import run_model_route_node, route_after_model_route
from langgraph.graph import END

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _state(session_mode: str, routing_decision: Optional[str] = "LOCAL_BIG") -> Dict[str, Any]:
    context_metrics = SimpleNamespace(
        routing_decision=routing_decision, task_complexity_index=62.0, css_total=78.0,
    ) if routing_decision is not None else None
    return {
        "task_id": "route-test",
        "session_permission_mode": session_mode,
        "context_metrics": context_metrics,
    }


def _config_with_review(review_fn: Any) -> RunnableConfig:
    return {"configurable": {"model_route_review_fn": review_fn}}


async def test_auto_mode_never_suspends_and_confirms_the_router_pick() -> None:
    # A review_fn that raises if called at all — AUTO must never reach it.
    never_call = AsyncMock(side_effect=AssertionError("AUTO must not suspend"))
    result = await run_model_route_node(_state("AUTO"), _config_with_review(never_call))
    assert result == {"confirmed_routing_decision": "LOCAL_BIG"}
    never_call.assert_not_awaited()


async def test_full_auto_also_bypasses() -> None:
    never_call = AsyncMock(side_effect=AssertionError("FULL_AUTO must not suspend"))
    result = await run_model_route_node(_state("FULL_AUTO"), _config_with_review(never_call))
    assert result["confirmed_routing_decision"] == "LOCAL_BIG"
    never_call.assert_not_awaited()


async def test_ask_mode_suspends_and_accept_confirms_the_drafted_decision() -> None:
    review_fn = AsyncMock(return_value={"approved": True})
    result = await run_model_route_node(_state("CAUTIOUS"), _config_with_review(review_fn))
    assert result == {"confirmed_routing_decision": "LOCAL_BIG", "hitl_pending": False}
    review_fn.assert_awaited_once()


async def test_plan_mode_also_suspends() -> None:
    review_fn = AsyncMock(return_value={"approved": True})
    result = await run_model_route_node(_state("PLAN_ONLY"), _config_with_review(review_fn))
    assert result["confirmed_routing_decision"] == "LOCAL_BIG"
    review_fn.assert_awaited_once()


async def test_override_with_modified_content_wins_over_the_drafted_decision() -> None:
    review_fn = AsyncMock(return_value={"approved": True, "modified_content": "LOCAL_SMALL"})
    result = await run_model_route_node(_state("CAUTIOUS"), _config_with_review(review_fn))
    assert result["confirmed_routing_decision"] == "LOCAL_SMALL"


async def test_cancel_clears_the_confirmed_decision_and_sets_hitl_pending() -> None:
    review_fn = AsyncMock(return_value={"approved": False})
    result = await run_model_route_node(_state("CAUTIOUS"), _config_with_review(review_fn))
    assert result == {"confirmed_routing_decision": None, "hitl_pending": True}


async def test_missing_context_metrics_degrades_to_the_default_decision() -> None:
    never_call = AsyncMock(side_effect=AssertionError("must not suspend in AUTO"))
    result = await run_model_route_node(_state("AUTO", routing_decision=None), _config_with_review(never_call))
    assert result == {"confirmed_routing_decision": "LOCAL_BIG"}


async def test_the_review_function_resolves_exactly_once_per_call() -> None:
    """The single-phase design's whole premise: resolving is cheap enough to
    redo on every replay. This pins the review function to exactly one call
    per invocation of the node — a regression that somehow called it twice
    (e.g. a stray retry loop) would silently double-suspend the operator."""
    review_fn = AsyncMock(return_value={"approved": True})
    await run_model_route_node(_state("CAUTIOUS"), _config_with_review(review_fn))
    assert review_fn.await_count == 1


def test_route_after_model_route_checks_hitl_pending_first() -> None:
    assert route_after_model_route({"hitl_pending": True}) == END
    assert route_after_model_route({"hitl_pending": False}) == "planner_agent"
    assert route_after_model_route({}) == "planner_agent"
