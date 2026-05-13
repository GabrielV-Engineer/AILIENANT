# ailienant-core/brain/drift_monitor.py
#
# Phase 2.2.C — Shadow Planner & Drift Monitor.
#
# Sits between planner_agent and route_to_coders in the LangGraph topology.
# On every invocation after the first turn it computes a text similarity score
# between the frozen baseline (immutable_wbs) and the current plan (mission_spec).
# If the plans diverge beyond _DRIFT_THRESHOLD, the graph pauses and surfaces a
# HITL approval request to the IDE user before continuing.

from __future__ import annotations

import difflib
import logging
from typing import Optional

from brain.state import MissionSpecification

logger = logging.getLogger("DRIFT_MONITOR")

# Similarity ratio below this value triggers the HITL gate.
# 0.70 means a plan that changed more than ~30% of its text is flagged.
_DRIFT_THRESHOLD: float = 0.70


def _plan_similarity(a: MissionSpecification, b: MissionSpecification) -> float:
    """Compute SequenceMatcher ratio between the full text of two plans.

    Concatenates outcome + all task descriptions so structural changes
    (renamed tasks, different target files) are captured alongside
    wording changes in the outcome statement.
    """
    text_a = a.outcome + " " + " ".join(t.description for t in a.tasks)
    text_b = b.outcome + " " + " ".join(t.description for t in b.tasks)
    return difflib.SequenceMatcher(None, text_a, text_b).ratio()


async def run_drift_monitor_node(state: dict) -> dict:
    """LangGraph node: compare current plan against the frozen baseline.

    Pass-through on first turn (immutable_wbs not yet set) or when plans
    are sufficiently similar. Triggers vfs_manager.request_human_approval()
    when semantic drift exceeds _DRIFT_THRESHOLD.

    Returns:
        {}                           — no drift, or first turn pass-through
        {"hitl_response": "timeout"} — HITL gate timed out; proceed with warning
        {"immutable_wbs": ..., ...}  — drift approved; new anchor written
        {"errors": [...], ...}       — drift rejected; error propagated to guardrail
    """
    immutable_wbs: Optional[MissionSpecification] = state.get("immutable_wbs")
    current_spec: Optional[MissionSpecification] = state.get("mission_spec")

    # First turn: immutable_wbs not yet frozen, nothing to compare.
    if immutable_wbs is None or current_spec is None:
        return {}

    sim = _plan_similarity(immutable_wbs, current_spec)

    if sim >= _DRIFT_THRESHOLD:
        logger.debug("DriftMonitor: plan similarity=%.2f — within threshold.", sim)
        return {}

    logger.warning(
        "DriftMonitor: semantic drift detected (similarity=%.2f < threshold=%.2f). "
        "Routing to HITL gate.",
        sim,
        _DRIFT_THRESHOLD,
    )

    # Deferred import to avoid circular dependency at module init time.
    from api.websocket_manager import vfs_manager

    response = await vfs_manager.request_human_approval(
        session_id=state.get("task_id", ""),
        action_description=(
            f"Plan drift detected (similarity={sim:.0%}). "
            f"The revised plan significantly deviates from the original. "
            f"Approve to continue with the new direction?"
        ),
        proposed_content=f"Revised outcome: {current_spec.outcome}",
    )

    if response is None:
        # HITL timed out (300s default) — proceed with a warning rather than blocking.
        logger.warning("DriftMonitor: HITL timeout — proceeding with revised plan.")
        return {"hitl_response": "timeout"}

    if response.get("approved"):
        logger.info(
            "DriftMonitor: drift approved by user — resetting immutable_wbs anchor."
        )
        return {
            "immutable_wbs": current_spec,
            "hitl_response": "approved",
            "hitl_pending": False,
        }

    # Rejected: propagate an error so the guardrail can route gracefully to END.
    logger.info("DriftMonitor: drift rejected by user — plan will need to be revised.")
    return {
        "hitl_response": "rejected",
        "hitl_pending": False,
        "errors": [
            f"Plan drift rejected by user (similarity={sim:.2f}). "
            "Re-planning required before proceeding."
        ],
    }
