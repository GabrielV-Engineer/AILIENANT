"""Phase 4.3 stage-2 — Circuit Breaker.

Fires when ``error_streak >= CIRCUIT_BREAKER_THRESHOLD (= 3)`` and
``cloud_surgeon_invocations < MAX_CLOUD_SURGEON (= 1)``. Effect: swap
``provider`` to ``CLOUD``, set ``active_llm_profile`` to the CLOUD_SURGEON
tier, increment the invocation counter. Caller (MICRO_SWARM router) re-dispatches
to CoderAgent once. A second failure after the swap emits
``CLOUD_SURGEON_EXHAUSTED`` and the caller routes to END.

Phase 6.3 (PHASE_6_BLUEPRINT §4.3) adds an orthogonal branch: when
``oom_fallback_active`` is set, an OOM rescue has already swung the turn to the
cloud fallback inside ``LLMGateway.ainvoke``. That branch acknowledges the
rescue WITHOUT charging ``error_streak`` or consuming the single Cloud Surgeon
shot — an OOM is a hardware/context failure, not a code-quality failure.

Blueprint reference: §4.3 (Circuit Breaker: Local → Cloud Surgeon).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from brain.state import LLMProfile

logger = logging.getLogger("CIRCUIT_BREAKER")

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

# Phase 6.3 — OOM fallback profile. Distinct sentinel from the Cloud Surgeon:
# an OOM rescue is a hardware/context failure, so it carries its own profile and
# never touches cloud_surgeon_invocations (Blueprint §4.3).
_OOM_CLOUD_PROFILE = LLMProfile(
    model_name="oom_cloud_fallback",
    parameters_b=0.0,
    context_window=200_000,
    quantization="cloud",
)


def evaluate_circuit_breaker(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return the channel deltas required to escalate to the Cloud Surgeon.

    Phase 6.3: if ``oom_fallback_active`` is set, an OOM cascade already swung
    this turn to the cloud fallback — acknowledge it, reset the flag, and return
    WITHOUT evaluating ``error_streak`` or spending the Cloud Surgeon shot.

    Empty dict means no escalation is warranted yet (streak below threshold).
    If the single Surgeon shot has already been spent and a fresh trip arrives,
    return only the ``CLOUD_SURGEON_EXHAUSTED`` security flag so the caller
    knows to route to END.
    """
    # Phase 6.3 — OOM Cascade orthogonality (Blueprint §4.3).
    if state.get("oom_fallback_active"):
        logger.warning(
            "OOM fallback acknowledged — bypassing error_streak evaluation; "
            "Cloud Surgeon shot NOT consumed."
        )
        return {
            "provider": "CLOUD",
            "active_llm_profile": _OOM_CLOUD_PROFILE,
            "oom_fallback_active": False,
        }

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
