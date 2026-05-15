# ailienant-core/brain/finops.py
#
# Phase 2.18 — FinOps Budget Gate Node.
#
# Sits between coder_agent and apply_patch in the LangGraph topology.
# Checks whether accumulated cost (current_cost_usd) exceeds the configured
# ceiling (max_budget_usd). If within budget, routes directly to apply_patch.
# If exceeded, opens a HITL gate:
#   - approved  → continue to apply_patch
#   - rejected  → route to END with error, hitl_response="budget_rejected"
#   - timeout   → route to END as fail-safe, hitl_response="budget_timeout"
#
# hitl_response values are namespaced ("budget_rejected", "budget_timeout") to
# avoid collision with drift_monitor's ("rejected", "timeout") values on the
# shared state field. See route_after_finops docstring for the full mapping.

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("FINOPS_GATE")

# Seconds to wait for a HITL response before treating as a timeout.
# Shorter than drift_monitor (300s) — budget decisions should be fast.
_FINOPS_HITL_TIMEOUT_S: float = 120.0


async def run_finops_node(state: dict) -> dict:
    """LangGraph node: enforce the FinOps budget ceiling.

    Reads current_cost_usd and max_budget_usd from state. If within budget,
    returns {} immediately (zero-cost pass-through). If budget exceeded,
    surfaces a HITL approval request.

    Returns:
        {}
            — budget OK; no state change.
        {"hitl_response": "approved", "hitl_pending": False}
            — budget exceeded but user approved continuing.
        {"hitl_response": "budget_rejected", "hitl_pending": False, "errors": [...]}
            — user rejected the overrun; route_after_finops sends this to END.
        {"hitl_response": "budget_timeout", "hitl_pending": False, "errors": [...]}
            — HITL timed out; fail-safe route to END prevents silent budget overflow.
    """
    current_cost: float = state.get("current_cost_usd", 0.0)
    max_budget: float = state.get("max_budget_usd", float("inf"))

    if current_cost <= max_budget:
        logger.debug(
            "FinOps: cost=%.4f USD <= budget=%.4f USD — pass-through.",
            current_cost, max_budget,
        )
        return {}

    logger.warning(
        "FinOps: budget EXCEEDED — current=%.4f USD, max=%.4f USD (task=%s). "
        "Opening HITL gate.",
        current_cost, max_budget, state.get("task_id", "unknown"),
    )

    # Deferred import mirrors drift_monitor.py pattern (avoids circular import at module init).
    from api.websocket_manager import vfs_manager

    response: Optional[dict] = await vfs_manager.request_human_approval(
        session_id=state.get("task_id", ""),
        action_description=(
            f"Budget ceiling exceeded: ${current_cost:.4f} USD spent, "
            f"limit is ${max_budget:.4f} USD. "
            f"Approve to continue (cost will increase further)?"
        ),
        proposed_content=f"Current spend: ${current_cost:.4f} | Budget: ${max_budget:.4f}",
        timeout_s=int(_FINOPS_HITL_TIMEOUT_S),
    )

    if response is None:
        # Timeout — fail-safe: route to END; do NOT silently proceed past budget.
        logger.error(
            "FinOps: HITL gate TIMED OUT after %.0fs for task=%s "
            "(cost=%.4f, budget=%.4f) — routing to END as fail-safe.",
            _FINOPS_HITL_TIMEOUT_S, state.get("task_id", "unknown"),
            current_cost, max_budget,
        )
        return {
            "hitl_response": "budget_timeout",
            "hitl_pending": False,
            "errors": [
                f"FinOps: HITL timeout after {_FINOPS_HITL_TIMEOUT_S:.0f}s — "
                f"halted to prevent silent budget overflow "
                f"(cost=${current_cost:.4f}, budget=${max_budget:.4f})."
            ],
        }

    if response.get("approved"):
        logger.info("FinOps: budget overrun approved by user — continuing execution.")
        return {"hitl_response": "approved", "hitl_pending": False}

    logger.info(
        "FinOps: budget overrun rejected by user — halting execution. "
        "Comment: %s", response.get("comment", "none"),
    )
    return {
        "hitl_response": "budget_rejected",
        "hitl_pending": False,
        "errors": [
            f"FinOps: execution rejected due to budget overrun "
            f"(cost=${current_cost:.4f}, budget=${max_budget:.4f}). "
            f"Comment: {response.get('comment', 'none')}"
        ],
    }


def route_after_finops(state: dict) -> str:
    """Synchronous conditional edge: route to apply_patch or END.

    LangGraph conditional edge functions must be synchronous even when the
    preceding node is async.

    Routes to END ("__end__") when:
        hitl_response == "budget_rejected"  — user rejected the overrun
        hitl_response == "budget_timeout"   — HITL timed out (fail-safe)

    Routes to "apply_patch" for all other values, including:
        None            — budget was OK, run_finops_node returned {}
        "approved"      — user approved the budget overrun
        "rejected"      — drift_monitor's value (must NOT trigger finops END)
        "timeout"       — drift_monitor's value (must NOT trigger finops END)

    Namespace isolation: "budget_rejected" / "budget_timeout" (finops) are
    distinct from "rejected" / "timeout" (drift_monitor) to prevent false END
    routing when this edge reads drift_monitor's prior hitl_response value.
    """
    from core.telemetry import log_routing_decision
    hitl_response: Optional[str] = state.get("hitl_response")
    if hitl_response in ("budget_rejected", "budget_timeout"):
        target = "__end__"
    else:
        target = "apply_patch"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="finops_gate",
        target=target,
        reason=f"hitl_response={hitl_response!r}",
        css=state.get("css"),
        tci=state.get("tci"),
    )
    if target == "__end__":
        logger.info("route_after_finops: %s → END.", hitl_response)
        return "__end__"
    logger.debug("route_after_finops: %r → apply_patch.", hitl_response)
    return "apply_patch"
