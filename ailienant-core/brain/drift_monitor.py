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
from typing import Any, Dict, Optional

from brain.state import MissionSpecification

logger = logging.getLogger("DRIFT_MONITOR")

# Similarity ratio below this value triggers the HITL gate.
# 0.70 means ~30% combined drift (text + structure) is flagged.
_DRIFT_THRESHOLD: float = 0.70

# Seconds to wait for a HITL response before timing out and proceeding.
# Reduce this value if long HITL waits are blocking the graph unexpectedly.
_DRIFT_TIMEOUT_S: int = 300


def _plan_similarity(a: MissionSpecification, b: MissionSpecification) -> float:
    """Hybrid similarity score combining textual and structural plan dimensions.

    Four components weighted to reduce both false-positives (plans that look
    different on the surface but target the same work) and false-negatives
    (plans with similar wording but different scope):

      text  50% — SequenceMatcher on outcome + task descriptions
      files 30% — Jaccard overlap of target_file sets
      count 10% — ratio of task counts (min/max)
      actions 10% — Jaccard overlap of action type sets

    Logs a DEBUG breakdown so operators can see exactly why a drift was flagged.
    """
    # Text similarity
    text_a = a.outcome + " " + " ".join(t.description for t in a.tasks)
    text_b = b.outcome + " " + " ".join(t.description for t in b.tasks)
    text_sim = difflib.SequenceMatcher(None, text_a, text_b).ratio()

    # Target file overlap (Jaccard)
    files_a = {t.target_file for t in a.tasks}
    files_b = {t.target_file for t in b.tasks}
    file_sim = len(files_a & files_b) / len(files_a | files_b) if (files_a or files_b) else 1.0

    # Task count ratio
    count_a, count_b = len(a.tasks), len(b.tasks)
    count_sim = (min(count_a, count_b) / max(count_a, count_b)) if max(count_a, count_b) > 0 else 1.0

    # Action type overlap (Jaccard)
    actions_a = {t.action for t in a.tasks}
    actions_b = {t.action for t in b.tasks}
    action_sim = len(actions_a & actions_b) / len(actions_a | actions_b) if (actions_a or actions_b) else 1.0

    combined = 0.50 * text_sim + 0.30 * file_sim + 0.10 * count_sim + 0.10 * action_sim
    logger.debug(
        "DriftMonitor: similarity — text=%.2f, files=%.2f, count=%.2f, actions=%.2f → combined=%.2f",
        text_sim, file_sim, count_sim, action_sim, combined,
    )
    return combined


async def run_drift_monitor_node(state: Dict[str, Any]) -> Dict[str, Any]:
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
        timeout_s=_DRIFT_TIMEOUT_S,
        request_kind="DRIFT_DETECTED",
    )

    if response is None:
        # HITL gate timed out — log at ERROR so this is not missed in production.
        logger.error(
            "DriftMonitor: HITL gate TIMED OUT after %ds for task=%s (similarity=%.2f) — "
            "proceeding with revised plan. To reduce silent bypasses, lower _DRIFT_TIMEOUT_S "
            "or investigate client connectivity.",
            _DRIFT_TIMEOUT_S,
            state.get("task_id", "unknown"),
            sim,
        )
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
