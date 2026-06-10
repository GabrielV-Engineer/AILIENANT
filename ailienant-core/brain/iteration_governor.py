"""Multi-axis circuit breaker for the autonomous ReAct execution cell.

Provides a pure, stateless three-axis check that replaces the single-axis step ceiling
shipped in 7.19.2, plus a cost estimator that implements the formal billing formula:

    Cost_total = Σ(C_in · T_in + C_out · T_out)

where T_in / T_out are the input and output tokens for one LLM call respectively.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class AxisExhausted(str, Enum):
    """Identifies which budget axis caused the circuit breaker to trip."""

    STEPS = "budget_steps"
    TOKENS = "budget_tokens"
    TIME = "budget_time"


def check_governor(
    *,
    step: int,
    cost_usd: float,
    elapsed_s: float,
    max_steps: int,
    max_cost_usd: float,
    max_elapsed_s: float,
) -> Optional[AxisExhausted]:
    """Pure three-axis circuit-breaker check.

    Returns the first exhausted axis, or ``None`` if the iteration may proceed.
    Axis priority: STEPS (free counter compare) → TIME (one monotonic read already
    computed by caller) → TOKENS (float compare against accumulated cost). All three
    must pass for None to be returned.
    """
    if step >= max_steps:
        return AxisExhausted.STEPS
    if elapsed_s >= max_elapsed_s:
        return AxisExhausted.TIME
    if cost_usd >= max_cost_usd:
        return AxisExhausted.TOKENS
    return None


def estimate_iteration_cost(
    input_messages: Sequence[Mapping[str, Any]],
    output_tool_calls: Sequence[Any],
) -> float:
    """Full-billing cost estimate for one iteration: C_in · T_in + C_out · T_out.

    ``input_messages`` must be the *full context* sent in this LLM call — LLM APIs
    are stateless, so the provider charges for every token in the call, not just the
    incremental delta relative to the previous turn.  The ``operator.add`` reducer on
    ``current_cost_usd`` in ``AIlienantGraphState`` therefore accumulates the growing
    context cost across iterations, correctly modelling actual billing.

    ``output_tool_calls`` is the model's response serialized to JSON, used to estimate
    completion tokens.  Output tokens typically cost 3–5× input tokens, so omitting
    them would significantly undercount cost on verbose edit responses.
    """
    from core.token_ledger import _USD_PER_K_CLOUD, _USD_PER_K_CLOUD_OUT
    from shared.token_counter import count_tokens

    input_text = " ".join(str(m.get("content", "")) for m in input_messages)
    output_text = " ".join(
        json.dumps({"name": tc.name, "args": tc.args}) for tc in output_tool_calls
    )
    t_in = count_tokens(input_text)
    t_out = count_tokens(output_text)
    return (t_in * _USD_PER_K_CLOUD + t_out * _USD_PER_K_CLOUD_OUT) / 1000.0
