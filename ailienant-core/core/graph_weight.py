# core/graph_weight.py
"""Graph Weight Calculator — a pre-execution context-OOM predictor.

Estimates the token weight of the graph state *before* a prompt is sent so the
router can pre-emptively move a request off a local model that would overflow its
context window. The predictor is pure and bounded: it counts, it never injects.

The budget ceiling is the *candidate model's* real window, not a fixed cloud
default — a state that is safe for a 128k cloud window can still destroy an 8k
local window, and only the local window predicts the real backend overflow.
"""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel

from tools.token_counter import PrecisionTokenCounter

# Reserve headroom for the model's own response, matching the prompt-builder budget.
_BUDGET_RATIO: float = 0.8

# Keys whose values carry the bulk of a turn's token weight.
_WEIGHT_KEYS: tuple[str, ...] = ("messages", "mission_spec", "vfs_buffer")


class GraphWeightEstimate(BaseModel):
    """Immutable verdict from estimate_graph_weight."""

    model_config = {"frozen": True}

    estimated_tokens: int
    budget_tokens: int
    overflow_risk: bool


def _stringify(value: Any) -> str:
    """Best-effort flatten of a state value to countable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, Mapping):
        return "\n".join(_stringify(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_stringify(v) for v in value)
    # VFSFile-like / dataclass-like objects: prefer an explicit content attribute.
    content = getattr(value, "content", None)
    if content is not None:
        return _stringify(content)
    return str(value)


def estimate_graph_weight(
    state: Mapping[str, Any],
    *,
    model_context_window: int,
    model_name: str = "gpt-4",
) -> GraphWeightEstimate:
    """Predict whether the state overflows the candidate model's context window.

    ``model_name`` selects only the tiktoken *counting* encoder (gpt-4's encoder is
    a fine proxy). ``model_context_window`` is the *budget ceiling* and MUST be the
    candidate model's real window — pass the local target's window when the routing
    decision is LOCAL_*, never a cloud default.
    """
    text = "\n".join(_stringify(state.get(key)) for key in _WEIGHT_KEYS)
    estimated = PrecisionTokenCounter.estimate_with_buffer(text, model_name) if text.strip() else 0
    window = max(1, int(model_context_window))
    budget = int(window * _BUDGET_RATIO)
    return GraphWeightEstimate(
        estimated_tokens=estimated,
        budget_tokens=budget,
        overflow_risk=estimated > budget,
    )
