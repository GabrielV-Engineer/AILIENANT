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
from typing import Any, Dict, Optional

logger = logging.getLogger("FINOPS_GATE")


async def run_finops_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: enforce the FinOps budget ceiling.

    Reads current_cost_usd and max_budget_usd from state. If within budget,
    returns {} immediately (zero-cost pass-through). If budget exceeded, suspends the
    graph for a human approval via native ``interrupt()`` (no coroutine is pinned — the
    run checkpoints and frees the runtime until the user replies).

    The gate decides on already-committed state (cost vs. budget), so a node replay on
    resume re-reaches the same ``interrupt()`` deterministically; nothing irreversible
    happens before it.

    Returns:
        {}
            — budget OK; no state change.
        {"hitl_response": "approved", "hitl_pending": False}
            — budget exceeded but user approved continuing.
        {"hitl_response": "budget_rejected", "hitl_pending": False, "errors": [...]}
            — user rejected the overrun; route_after_finops sends this to END.
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

    # Native suspend: interrupt() checkpoints the graph and frees the runtime until the
    # user replies — no coroutine is pinned on a wall-clock deadline.
    from core.hitl import request_graph_approval

    response: Dict[str, Any] = request_graph_approval(
        session_id=state.get("task_id", ""),
        action_description=(
            f"Budget ceiling exceeded: ${current_cost:.4f} USD spent, "
            f"limit is ${max_budget:.4f} USD. "
            f"Approve to continue (cost will increase further)?"
        ),
        proposed_content=f"Current spend: ${current_cost:.4f} | Budget: ${max_budget:.4f}",
        request_kind="BUDGET_CEILING",
    )

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


def route_after_finops(state: Dict[str, Any]) -> str:
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
