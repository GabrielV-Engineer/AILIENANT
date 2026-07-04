# ailienant-core/brain/dispatch_emitter.py
"""Optional DispatchPlan emission for the planner / researcher nodes.

A dispatching agent may, alongside its normal output, emit a validated
``DispatchPlan`` that fans work out to subagents. This module isolates that decision
so the core planning path stays untouched: the agent node calls
``maybe_emit_dispatch`` and merges the returned delta into its own.

Resolution order (first non-empty wins):
  1. An injected ``config.configurable["dispatch_plan_fn"]`` — the seam tests and
     benchmarks use to drive emission deterministically (sync or async; returns a
     plan dict / ``DispatchPlan`` / ``None``).
  2. A synthetic plan under ``AILIENANT_DISPATCH_DEBUG`` — a fixed, valid plan so the
     production node path is exercisable without a live gateway.
  3. Otherwise nothing (production autonomous LLM-driven emission is deferred —
     DEBT-107; the mechanism, guards, and graph wiring are complete and consume a plan
     from either seam above or a directly-seeded ``dispatch_plan`` channel).

Emission is a no-op unless dynamic dispatch is enabled, so a default deployment never
pays for the decision. A malformed candidate validates to nothing rather than raising —
a bad emission must never crash the planning turn.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Dict, Mapping, Optional

from shared.config import ENABLE_DYNAMIC_DISPATCH
from brain.subagent_contracts import DispatchPlan

logger = logging.getLogger("DISPATCH_EMITTER")


def _fresh_dispatch_channels(plan: Dict[str, Any], return_node: str) -> Dict[str, Any]:
    """The state delta that opens a dispatch: the plan, its return target, and a full
    reset of the per-dispatch counters. ``_dispatch_consumed`` is deliberately NOT reset
    — the watermark must persist so this dispatch's baseline is the previous synthesis's
    advance (else a prior dispatch's envelopes would be re-digested)."""
    return {
        "dispatch_plan": plan,
        "dispatch_return_node": return_node,
        "dispatch_wave_count": 0,
        "dispatch_round_count": 0,
        "_dispatch_reserved_usd": 0.0,
        "_dispatch_reserved_round": -1,  # sentinel: nothing reserved yet this dispatch
        "_dispatch_admission": None,
    }


def _synthetic_plan() -> Dict[str, Any]:
    """A minimal valid fan-out plan for AILIENANT_DISPATCH_DEBUG exercises."""
    return DispatchPlan.model_validate(
        {
            "pattern": "fanout_and_synthesize",
            "tasks": [
                {
                    "task_id": "debug-dispatch-1",
                    "description": "Synthetic dispatch task (AILIENANT_DISPATCH_DEBUG).",
                    "subagent_role": "analyst_readonly",
                    "response_schema": {"fields": [{"name": "summary", "type": "str", "description": "one line"}]},
                    "context_refs": [],
                    "max_iterations": 1,
                }
            ],
            "synthesis_instruction": "Summarize the subagent findings.",
            "dispatch_depth": 0,
        }
    ).model_dump()


async def maybe_emit_dispatch(
    state: Mapping[str, Any],
    config: Optional[Mapping[str, Any]],
    *,
    return_node: str,
) -> Dict[str, Any]:
    """Return a dispatch-opening state delta, or ``{}`` when no fan-out is warranted."""
    if not ENABLE_DYNAMIC_DISPATCH:
        return {}

    candidate: Any = None
    configurable = (config or {}).get("configurable", {}) if config else {}
    hook = configurable.get("dispatch_plan_fn")
    if hook is not None:
        try:
            candidate = hook(state)
            if inspect.isawaitable(candidate):
                candidate = await candidate
        except Exception as exc:  # noqa: BLE001 — a faulty injected hook must not crash the turn
            logger.warning("dispatch_plan_fn hook failed; no dispatch emitted: %s", exc)
            candidate = None

    if candidate is None and os.getenv("AILIENANT_DISPATCH_DEBUG", "0") != "0":
        candidate = _synthetic_plan()

    if candidate is None:
        return {}

    # Validate whatever the seam produced; a malformed plan emits nothing.
    try:
        raw = candidate.model_dump() if isinstance(candidate, DispatchPlan) else dict(candidate)
        plan = DispatchPlan.model_validate(raw).model_dump()
    except Exception as exc:  # noqa: BLE001 — never raise out of an optional emission
        logger.warning("discarding malformed emitted dispatch plan: %s", exc)
        return {}

    logger.info("dispatch emitted: pattern=%s tasks=%d return=%s",
                plan.get("pattern"), len(plan.get("tasks", [])), return_node)
    return _fresh_dispatch_channels(plan, return_node)
