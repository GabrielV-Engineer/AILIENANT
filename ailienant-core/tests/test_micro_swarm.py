# ailienant-core/tests/test_micro_swarm.py
"""Phase 4.3 stage-2 — MICRO_SWARM topology unit tests.

Exercises the routing functions and the circuit-breaker node directly, plus
one end-to-end happy-path run through a freshly compiled graph whose
CoderAgent / SyntaxGate / StyleGate are replaced with deterministic stubs.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest
from langgraph.graph import END

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Routing-function unit tests (synchronous, fast)
# ---------------------------------------------------------------------------


def test_route_after_style_latched_bypass_goes_to_end() -> None:
    """style_bypass_active=True must route to END regardless of failure counts."""
    from brain.swarms import _route_after_style

    state = {"style_bypass_active": True, "consecutive_style_failures": 7}
    assert _route_after_style(state) == END


def test_route_after_style_zero_failures_goes_to_end() -> None:
    """consecutive_style_failures == 0 means StyleGate just passed → END."""
    from brain.swarms import _route_after_style

    state = {"style_bypass_active": False, "consecutive_style_failures": 0}
    assert _route_after_style(state) == END


def test_route_after_circuit_breaker_ignores_retry_count() -> None:
    """retry_count is OWNED by Orchestrator — MICRO_SWARM must ignore it.

    Even with retry_count pre-seeded at 99 (a sentinel value an outer caller
    might leave behind), the router still loops back to coder_agent because
    the Cloud Surgeon shot has not been exhausted yet. This proves the
    ownership boundary: retry_count is outer-loop state, NOT inner-loop state.
    """
    from brain.swarms import _route_after_circuit_breaker

    state = {"retry_count": 99, "security_flags": [], "error_streak": 1}
    assert _route_after_circuit_breaker(state) == "coder_agent"


def test_route_after_circuit_breaker_exhausted_goes_to_end() -> None:
    """CLOUD_SURGEON_EXHAUSTED in flags must terminate the MICRO_SWARM."""
    from brain.swarms import _route_after_circuit_breaker

    state = {"security_flags": ["CLOUD_SURGEON_EXHAUSTED"], "error_streak": 4}
    assert _route_after_circuit_breaker(state) == END


# ---------------------------------------------------------------------------
# Circuit-breaker node behaviour
# ---------------------------------------------------------------------------


async def test_circuit_breaker_node_bumps_streak_no_swap_below_threshold() -> None:
    """error_streak below threshold → bump counter, no tier swap."""
    from brain.swarms import _circuit_breaker_node

    deltas = await _circuit_breaker_node({"error_streak": 1})
    assert deltas == {"error_streak": 2}


async def test_circuit_breaker_node_trips_swap_on_threshold() -> None:
    """At streak == 3 the Surgeon must be swapped in exactly once."""
    from brain.swarms import _circuit_breaker_node

    deltas = await _circuit_breaker_node(
        {"error_streak": 2, "cloud_surgeon_invocations": 0}
    )
    assert deltas["error_streak"] == 3
    assert deltas["circuit_breaker_tripped"] is True
    assert deltas["provider"] == "CLOUD"
    assert deltas["cloud_surgeon_invocations"] == 1


async def test_circuit_breaker_node_emits_exhausted_after_swap() -> None:
    """A second trip after the Surgeon shot has been spent emits the EXHAUSTED flag."""
    from brain.swarms import _circuit_breaker_node

    deltas = await _circuit_breaker_node(
        {"error_streak": 3, "cloud_surgeon_invocations": 1}
    )
    assert deltas["error_streak"] == 4
    assert "CLOUD_SURGEON_EXHAUSTED" in deltas.get("security_flags", [])
    assert "circuit_breaker_tripped" not in deltas  # tripped channel NOT re-asserted


# ---------------------------------------------------------------------------
# End-to-end happy path through a freshly compiled graph
# ---------------------------------------------------------------------------


async def test_happy_path_exits_clean() -> None:
    """coder ok → syntax pass → style pass → END with error_streak untouched.

    Rebuilds the graph with stubbed nodes so we exercise the compiled topology
    (not just the routing functions). Patches the deferred-import sites
    inside build_micro_swarm so the fresh compilation picks them up.
    """
    async def fake_coder(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"messages": [{"role": "assistant", "content": "wrote it"}]}

    async def fake_syntax(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"syntax_gate_status": "pass"}

    async def fake_style(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"consecutive_style_failures": 0}

    with (
        patch("agents.coder.run_coder_node", new=fake_coder),
        patch("validators.gates.syntax_gate_node", new=fake_syntax),
        patch("validators.gates.style_gate_node", new=fake_style),
    ):
        from brain.swarms import build_micro_swarm

        app = build_micro_swarm()
        result = await app.ainvoke(
            {
                "messages": [{"role": "user", "content": "do it"}],
                "error_streak": 0,
                "consecutive_style_failures": 0,
                "cloud_surgeon_invocations": 0,
                "style_bypass_active": False,
                "syntax_gate_status": "pending",
            }
        )

    assert result["syntax_gate_status"] == "pass"
    assert result["consecutive_style_failures"] == 0
    assert result.get("error_streak", 0) == 0
    assert any(m["content"] == "wrote it" for m in result["messages"])
