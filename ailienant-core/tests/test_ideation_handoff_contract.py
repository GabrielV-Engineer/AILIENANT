# ailienant-core/tests/test_ideation_handoff_contract.py
#
# Regression guard for the ideation -> planner handoff going permanently dark.
#
# root cause: run_synthesis_node signals the handoff via "ideation_synthesized",
# a key that was never declared on AIlienantGraphState. LangGraph's compiled
# StateGraph silently drops any node-returned key that isn't a declared channel
# (langgraph/graph/state.py::attach_node._get_updates) -- no exception, no
# warning. route_after_ideation then saw the flag as absent on every single run
# and fell to its defensive "ideation_no_op" branch, ending the turn at END
# without ever invoking the planner. Every existing test in test_ideation.py
# asserts against hand-built dicts or a node's own isolated return value, never
# against the real compiled graph -- exactly the seam this file exercises.

from typing import Any, Dict

import pytest

import agents.analyst as analyst_mod
import brain.ideation as ideation_mod
from agents.analyst import run_analyst_node
from brain.engine import route_after_ideation
from brain.ideation import ideation_graph, run_synthesis_node
from brain.state import AIlienantGraphState, assert_declared_channels


@pytest.fixture
def _force_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deterministic synthetic path so no live LLM call is needed."""
    monkeypatch.setattr(analyst_mod, "DEBUG_MODE", True)
    monkeypatch.setattr(ideation_mod, "DEBUG_MODE", True)


# ---------------------------------------------------------------------------
# Integration: drive the REAL compiled subgraph, not a hand-built dict.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agreement_turn_survives_the_compiled_graph_boundary(
    _force_debug: None,
) -> None:
    """An agreement turn must reach planner_agent through the real graph.

    This is the exact regression: before ideation_synthesized/ideation_glossary
    were declared on AIlienantGraphState, this call returned ideation_synthesized
    as absent (dropped at the LangGraph channel-write boundary), so
    route_after_ideation on the real output fell to END instead of planner_agent.
    """
    initial_state: Dict[str, Any] = {
        "task_id": "handoff-sess",
        "user_input": "looks good, let's proceed",
        "messages": [{"role": "assistant", "content": "Does this plan look solid?"}],
    }

    result = await ideation_graph.ainvoke(initial_state)  # type: ignore[call-overload]  # pyright: ignore[reportArgumentType] — dict test double for the AIlienantGraphState TypedDict

    assert result.get("ideation_synthesized") is True
    assert route_after_ideation(result) == "planner_agent"


@pytest.mark.anyio
async def test_ideation_glossary_survives_the_compiled_graph_boundary() -> None:
    """The distilled ubiquitous language must reach the planner, not just the flag.

    planner.py:817 reads state.get("ideation_glossary") -- before it was declared,
    this was always None regardless of what the dialogue actually settled.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    import json

    brief = {
        "intent": "Build a JWT auth service.",
        "constraints": [],
        "scope_hints": [],
        "ubiquitous_language": {"token": "a signed JWT"},
    }
    llm_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(brief)))]
    )
    initial_state: Dict[str, Any] = {
        "task_id": "handoff-sess-2",
        "user_input": "looks good, let's proceed",
        "messages": [{"role": "assistant", "content": "What auth scheme?"}],
    }
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=llm_response),
    ):
        result = await ideation_graph.ainvoke(initial_state)  # type: ignore[call-overload]  # pyright: ignore[reportArgumentType] — dict test double for the AIlienantGraphState TypedDict

    assert result.get("ideation_synthesized") is True
    assert result.get("ideation_glossary") == {"token": "a signed JWT"}


# ---------------------------------------------------------------------------
# Contract: every key a node writes must be a declared channel.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_synthesis_node_only_writes_declared_channels(_force_debug: None) -> None:
    state: Dict[str, Any] = {
        "messages": [
            {"role": "assistant", "content": "What is the primary deliverable?"},
            {"role": "user", "content": "A working auth service with JWT."},
        ],
        "user_input": "looks good",
    }
    result = await run_synthesis_node(state)
    undeclared = set(result) - set(AIlienantGraphState.__annotations__)
    assert not undeclared, f"synthesis_node wrote undeclared channel(s): {undeclared}"


@pytest.mark.anyio
async def test_analyst_node_only_writes_declared_channels(_force_debug: None) -> None:
    state: Dict[str, Any] = {
        "task_id": "test-sess",
        "user_input": "Build me a REST API",
        "messages": [],
    }
    result = await run_analyst_node(state)
    undeclared = set(result) - set(AIlienantGraphState.__annotations__)
    assert not undeclared, f"analyst_grill wrote undeclared channel(s): {undeclared}"


# ---------------------------------------------------------------------------
# Guard: assert_declared_channels itself catches a stray key.
# ---------------------------------------------------------------------------


def test_assert_declared_channels_raises_on_unknown_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AILIENANT_STRICT_STATE", "1")
    with pytest.raises(ValueError, match="totally_bogus_channel"):
        assert_declared_channels("some_node", {"totally_bogus_channel": True})


def test_assert_declared_channels_accepts_declared_keys() -> None:
    assert_declared_channels(
        "synthesis_node",
        {"ideation_synthesized": True, "ideation_glossary": {}},
    )


def test_assert_declared_channels_ignores_non_dict_deltas() -> None:
    assert_declared_channels("some_node", None)
