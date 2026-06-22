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


async def run_drift_compute_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: compute plan-drift similarity ONCE and commit the gate decision.

    Split from the gate so the interrupt-bearing node (``drift_gate``) decides on
    already-committed state. A node that calls ``interrupt()`` discards its own
    pre-interrupt writes, and the similarity score (SequenceMatcher + Jaccard over a
    possibly reordered task set) is not bitwise-stable across a replay — recomputing it
    in the interrupting node could straddle the threshold and orphan the resume. So the
    decision is produced here and committed to the checkpoint before the gate runs.

    Returns:
        {"drift_gate_open": False}                          — first turn or within threshold
        {"drift_gate_open": True, "drift_similarity": float} — drift flagged; gate will ask
    """
    immutable_wbs: Optional[MissionSpecification] = state.get("immutable_wbs")
    current_spec: Optional[MissionSpecification] = state.get("mission_spec")

    # First turn: immutable_wbs not yet frozen, nothing to compare.
    if immutable_wbs is None or current_spec is None:
        return {"drift_gate_open": False}

    sim = _plan_similarity(immutable_wbs, current_spec)
    if sim >= _DRIFT_THRESHOLD:
        logger.debug("DriftMonitor: plan similarity=%.2f — within threshold.", sim)
        return {"drift_gate_open": False}

    logger.warning(
        "DriftMonitor: semantic drift detected (similarity=%.2f < threshold=%.2f). "
        "Gate will request human approval.",
        sim,
        _DRIFT_THRESHOLD,
    )
    return {"drift_gate_open": True, "drift_similarity": sim}


async def run_drift_gate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: suspend for human approval when ``drift_compute`` opened the gate.

    Reads the **committed** ``drift_gate_open`` / ``drift_similarity`` (deterministic on
    replay) and, when open, makes ``interrupt()`` its first action — so a replay before
    resume is side-effect-free and the approval resumes cleanly.

    Returns:
        {}                              — gate closed (no drift / first turn)
        {"immutable_wbs": ..., ...}     — drift approved; new anchor written
        {"errors": [...], ...}          — drift rejected; error propagated to guardrail
    """
    if not state.get("drift_gate_open"):
        return {}

    sim = float(state.get("drift_similarity") or 0.0)
    current_spec: Optional[MissionSpecification] = state.get("mission_spec")

    # Native suspend (interrupt-first): checkpoints the graph and frees the runtime.
    from core.hitl import request_graph_approval

    response = request_graph_approval(
        session_id=state.get("task_id", ""),
        action_description=(
            f"Plan drift detected (similarity={sim:.0%}). "
            f"The revised plan significantly deviates from the original. "
            f"Approve to continue with the new direction?"
        ),
        proposed_content=(
            f"Revised outcome: {current_spec.outcome}" if current_spec is not None else None
        ),
        request_kind="DRIFT_DETECTED",
    )

    if response.get("approved"):
        logger.info("DriftMonitor: drift approved by user — resetting immutable_wbs anchor.")
        return {
            "immutable_wbs": current_spec,
            "hitl_response": "approved",
            "hitl_pending": False,
            "drift_gate_open": False,
        }

    # Rejected: propagate an error so the guardrail can route gracefully to END.
    logger.info("DriftMonitor: drift rejected by user — plan will need to be revised.")
    return {
        "hitl_response": "rejected",
        "hitl_pending": False,
        "drift_gate_open": False,
        "errors": [
            f"Plan drift rejected by user (similarity={sim:.2f}). "
            "Re-planning required before proceeding."
        ],
    }
