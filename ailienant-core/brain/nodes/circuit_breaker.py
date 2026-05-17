"""Phase 4.3 stage-2 — Circuit Breaker.

Fires when ``error_streak >= CIRCUIT_BREAKER_THRESHOLD (= 3)`` and
``cloud_surgeon_invocations < MAX_CLOUD_SURGEON (= 1)``. Effect: swap
``provider`` to ``CLOUD``, set ``active_llm_profile`` to the CLOUD_SURGEON
tier, increment the invocation counter. Caller (MICRO_SWARM router) re-dispatches
to CoderAgent once. A second failure after the swap emits
``CLOUD_SURGEON_EXHAUSTED`` and the caller routes to END.

Blueprint reference: §4.3 (Circuit Breaker: Local → Cloud Surgeon).
"""
from __future__ import annotations

from typing import Any, Dict

from brain.state import LLMProfile

CIRCUIT_BREAKER_THRESHOLD: int = 3
MAX_CLOUD_SURGEON: int = 1

# Sentinel CLOUD_SURGEON profile — the channel-write payload the LLMGateway
# dispatches off of. Real model wiring (model name → endpoint) is shared/config
# responsibility; this object only carries the tier signal.
_CLOUD_SURGEON_PROFILE = LLMProfile(
    model_name="cloud_surgeon",
    parameters_b=0.0,
    context_window=200_000,
    quantization="cloud",
)


def evaluate_circuit_breaker(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return the channel deltas required to escalate to the Cloud Surgeon.

    Empty dict means no escalation is warranted yet (streak below threshold).
    If the single Surgeon shot has already been spent and a fresh trip arrives,
    return only the ``CLOUD_SURGEON_EXHAUSTED`` security flag so the caller
    knows to route to END.
    """
    streak = int(state.get("error_streak", 0))
    invocations = int(state.get("cloud_surgeon_invocations", 0))

    if streak < CIRCUIT_BREAKER_THRESHOLD:
        return {}
    if invocations >= MAX_CLOUD_SURGEON:
        return {"security_flags": ["CLOUD_SURGEON_EXHAUSTED"]}

    return {
        "circuit_breaker_tripped": True,
        "provider": "CLOUD",
        "active_llm_profile": _CLOUD_SURGEON_PROFILE,
        "cloud_surgeon_invocations": 1,
    }
