"""Phase 2.23 — Unit tests for ContractGuardNode (Event-Driven Context Anchoring).

DoD: covers the three deterministic triggers, pass-through behaviour, LLM stub
injection, and graceful fallback on LLM failure. Mirrors the pytest.mark.anyio
async pattern used across the suite.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from agents.contract_guard import (
    ContractGuardNode,
    SessionContract,
    run_contract_guard_node,
)
from brain.state import LLMProfile, TokenCounter


def _profile(window: int = 8000) -> LLMProfile:
    return LLMProfile(
        model_name="ailienant/medium",
        parameters_b=7.0,
        context_window=window,
        quantization="Q4_K_M",
    )


def _tokens(local: int = 0, cloud: int = 0) -> TokenCounter:
    return TokenCounter(local=local, cloud=cloud, total_cost_usd=0.0)


# ---------------------------------------------------------------------------
# _evaluate_triggers — pure deterministic logic (synchronous)
# ---------------------------------------------------------------------------


def test_trigger_none_on_quiet_turn() -> None:
    """Anchor matches state, CSS healthy → NONE."""
    state: Dict[str, Any] = {
        "tci": 30.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 28.0, "target_role": "Refactor", "turn": 1},
        "token_usage": _tokens(local=100),
        "active_llm_profile": _profile(),
    }
    assert ContractGuardNode._evaluate_triggers(state) == "NONE"


def test_trigger_tci_delta() -> None:
    """abs(40 - 20) = 20 > 15 → TCI_DELTA."""
    state: Dict[str, Any] = {
        "tci": 40.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 20.0, "target_role": "Refactor", "turn": 1},
    }
    assert ContractGuardNode._evaluate_triggers(state) == "TCI_DELTA"


def test_trigger_tci_delta_below_threshold_is_none() -> None:
    """abs(34 - 20) = 14 ≤ 15 → NONE (strict greater-than)."""
    state: Dict[str, Any] = {
        "tci": 34.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 20.0, "target_role": "Refactor", "turn": 1},
    }
    assert ContractGuardNode._evaluate_triggers(state) == "NONE"


def test_trigger_css_at_capacity_fires_without_anchor() -> None:
    """First turn, CSS=30 (<40) and 81% window usage → CSS_AT_CAPACITY."""
    state: Dict[str, Any] = {
        "tci": 10.0,
        "css": 30.0,
        "target_role": "Refactor",
        "contract_anchor": None,
        "token_usage": _tokens(local=6480),  # 6480/8000 = 0.81
        "active_llm_profile": _profile(window=8000),
    }
    assert ContractGuardNode._evaluate_triggers(state) == "CSS_AT_CAPACITY"


def test_trigger_css_low_but_window_not_saturated_is_none() -> None:
    """CSS<40 alone is not enough; needs 80%+ context window too."""
    state: Dict[str, Any] = {
        "tci": 10.0,
        "css": 20.0,
        "target_role": "Refactor",
        "contract_anchor": None,
        "token_usage": _tokens(local=100),  # 100/8000 ≈ 1.25%
        "active_llm_profile": _profile(),
    }
    assert ContractGuardNode._evaluate_triggers(state) == "NONE"


def test_trigger_subgraph_shift() -> None:
    """target_role changes between turns → SUBGRAPH_SHIFT."""
    state: Dict[str, Any] = {
        "tci": 30.0,
        "css": 80.0,
        "target_role": "SecOps",
        "contract_anchor": {"tci": 30.0, "target_role": "Refactor", "turn": 2},
    }
    assert ContractGuardNode._evaluate_triggers(state) == "SUBGRAPH_SHIFT"


def test_trigger_first_turn_no_anchor_returns_none_when_css_ok() -> None:
    """First turn (anchor=None) with healthy CSS → NONE — anchor-only triggers can't fire."""
    state: Dict[str, Any] = {
        "tci": 90.0,  # would trip delta, but no anchor to compare against
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": None,
        "token_usage": _tokens(local=100),
        "active_llm_profile": _profile(),
    }
    assert ContractGuardNode._evaluate_triggers(state) == "NONE"


# ---------------------------------------------------------------------------
# __call__ — async node behaviour
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_node_returns_empty_dict_on_none() -> None:
    """Quiet turn → pure pass-through, no state delta, no LLM call."""
    state: Dict[str, Any] = {
        "tci": 30.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 30.0, "target_role": "Refactor", "turn": 1},
        "token_usage": _tokens(local=100),
        "active_llm_profile": _profile(),
    }
    assert await run_contract_guard_node(state) == {}


@pytest.mark.anyio
async def test_node_emits_ui_payload_on_trigger() -> None:
    """Trigger fires → ui_payload with RENDER_PERSISTENT_CONTRACT + bumped anchor turn."""
    captured: Dict[str, Any] = {}

    async def stub_invoker(system: str, payload: Dict[str, Any]) -> str:
        captured["system"] = system
        captured["payload"] = payload
        return SessionContract(
            mission_outcome="Ship Phase 2.23",
            active_role="Refactor",
            in_scope=["agents/contract_guard.py"],
            out_of_scope=["brain/state.py"],
            open_constraints=["O(1) middleware"],
            trigger_reason="TCI_DELTA",
        ).model_dump_json()

    node = ContractGuardNode(llm_invoker=stub_invoker)
    state: Dict[str, Any] = {
        "tci": 50.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 20.0, "target_role": "Refactor", "turn": 3},
    }
    result = await node(state)

    assert result["ui_payload"]["action"] == "RENDER_PERSISTENT_CONTRACT"
    assert result["ui_payload"]["reason"] == "TCI_DELTA"
    assert result["ui_payload"]["contract"]["mission_outcome"] == "Ship Phase 2.23"
    assert result["contract_anchor"]["turn"] == 4
    assert result["contract_anchor"]["tci"] == 50.0
    assert result["contract_anchor"]["target_role"] == "Refactor"
    assert captured["payload"]["trigger_reason"] == "TCI_DELTA"


@pytest.mark.anyio
async def test_llm_failure_falls_back_to_skeleton() -> None:
    """LLM call raising → deterministic skeleton contract still emitted, anchor still bumped."""
    async def failing_invoker(system: str, payload: Dict[str, Any]) -> str:
        raise RuntimeError("LiteLLM proxy unreachable")

    node = ContractGuardNode(llm_invoker=failing_invoker)
    state: Dict[str, Any] = {
        "tci": 50.0,
        "css": 80.0,
        "target_role": "SecOps",
        "contract_anchor": {"tci": 20.0, "target_role": "Refactor", "turn": 1},
    }
    result = await node(state)

    assert result["ui_payload"]["action"] == "RENDER_PERSISTENT_CONTRACT"
    # Either TCI_DELTA or SUBGRAPH_SHIFT — both fire here; TCI_DELTA is checked first.
    assert result["ui_payload"]["reason"] in {"TCI_DELTA", "SUBGRAPH_SHIFT"}
    assert result["ui_payload"]["contract"]["active_role"] == "SecOps"
    assert result["contract_anchor"]["turn"] == 2


@pytest.mark.anyio
async def test_llm_returning_invalid_json_falls_back() -> None:
    """LLM returns malformed JSON → validation error → fallback skeleton."""
    async def bad_json_invoker(system: str, payload: Dict[str, Any]) -> str:
        return "not actually json {"

    node = ContractGuardNode(llm_invoker=bad_json_invoker)
    state: Dict[str, Any] = {
        "tci": 50.0,
        "css": 80.0,
        "target_role": "Refactor",
        "contract_anchor": {"tci": 20.0, "target_role": "Refactor", "turn": 0},
    }
    result = await node(state)

    assert result["ui_payload"]["contract"]["trigger_reason"] == "TCI_DELTA"
    assert result["contract_anchor"]["turn"] == 1
