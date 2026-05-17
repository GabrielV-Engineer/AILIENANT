# ailienant-core/agents/orchestrator.py
"""Phase 4.1.3 — OrchestratorAgent (Capataz / Runtime Controller).

Deterministic O(1) LangGraph node. No LLM call. Picks the next pending step from
the WBS, propagates Prompt Swap via state["target_role"], and enforces the
Bounded Failure ceiling (MAX_RETRIES = 2 per blueprint §4.1).

OWNERSHIP CONTRACT (load-bearing — do NOT change without amending Phase 4.1.3 DoD):
    * The Orchestrator is the JUDGE of retry_count, NOT its incrementer.
      ``retry_count += 1`` is the strict responsibility of the DOWNSTREAM failure
      evaluator (validate_output on validation failure, drift_monitor on drift,
      future AnalystAgent on QA rejection). If those nodes do not increment,
      the Bounded Failure branch never fires and a wired graph self-loops
      indefinitely. The standalone unit tests inject retry_count directly to
      simulate downstream behaviour.

Wiring into brain/engine.py is deferred to Phase 4.3 when the three execution
modes are assembled. The node is reachable directly via
    from agents.orchestrator import run_orchestrator_node
for unit testing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from brain.state import MissionSpecification, WBSStep

logger = logging.getLogger("ORCHESTRATOR_NODE")

# Reuse the blueprint's canonical retry ceiling. Same value as the MICRO_SWARM
# Coder loop and the Phase 1.0.5 Checkpoint Gate — different gate, same number.
MAX_RETRIES: int = 2

# Routing threshold inlined to avoid leaf-on-leaf imports against
# brain/routing_engine.py:39-45. Both sites move together when this changes.
_RED_ALERT_CSS: float = 40.0

# Mapped flag namespace for downstream consumers (HITL UI / DriftMonitor).
_FLAG_BOUNDED_FAILURE: str = "BOUNDED_FAILURE_LIMIT_REACHED"
_FLAG_RED_ALERT: str = "RED_ALERT_ORCHESTRATOR"
_FLAG_ALL_COMPLETE: str = "ALL_WBS_STEPS_COMPLETE"


def _pick_next_step(tasks: List[WBSStep]) -> Optional[WBSStep]:
    """Return the first task whose status is neither 'completed' nor 'failed'.

    Tasks in 'in_progress' are returned (intentional — a retry re-uses the same
    step). ``run_orchestrator_node`` is responsible for not redundantly
    re-mutating a step that is already 'in_progress' (R2 idempotency).
    """
    for step in tasks:
        if step.status not in ("completed", "failed"):
            return step
    return None


def _mark_step_status(
    mission: MissionSpecification, step_number: int, new_status: str
) -> MissionSpecification:
    """Immutable update: clone the mission with the named step's status mutated."""
    new_tasks: List[WBSStep] = [
        step.model_copy(update={"status": new_status})
        if step.step_number == step_number
        else step
        for step in mission.tasks
    ]
    return mission.model_copy(update={"tasks": new_tasks})


def _safe_get_css(metrics: Any, fallback: float) -> float:
    """Extract ``css_total`` from ``context_metrics`` tolerating both shapes.

    LangGraph SQLite checkpoint round-trip may deliver ``state["context_metrics"]``
    as either a ContextMeter Pydantic model OR a plain dict[str, Any]. A naive
    ``hasattr(metrics, "css_total")`` returns False on a dict (a key is not an
    attribute), so this helper branches on shape explicitly.
    """
    if metrics is None:
        return fallback
    if isinstance(metrics, dict):
        value = metrics.get("css_total")
        return float(value) if value is not None else fallback
    value = getattr(metrics, "css_total", None)
    return float(value) if value is not None else fallback


async def run_orchestrator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Capataz: lifecycle of the WBS. Deterministic, no LLM call.

    Behaviour matrix:
        - No mission_spec / no tasks      → return errors entry.
        - All tasks completed/failed      → return {ALL_WBS_STEPS_COMPLETE} signal.
        - Bounded Failure ceiling reached → mark active step 'failed',
                                            hitl_pending=True, retry_count=0.
        - Active step already in_progress → idempotent: emit target_role +
                                            current_step_id, skip mission mutation.
        - Otherwise                       → mark step in_progress, set
                                            target_role + current_step_id,
                                            optionally raise RED_ALERT flag.
    """
    mission: Optional[MissionSpecification] = state.get("mission_spec")
    if mission is None or not mission.tasks:
        logger.error("Orchestrator: no mission_spec or empty WBS — cannot proceed.")
        return {"errors": ["Orchestrator: missing mission_spec or empty tasks."]}

    active = _pick_next_step(mission.tasks)

    # --- Terminal state: all steps completed/failed ---
    if active is None:
        logger.info("Orchestrator: all WBS steps reached terminal state.")
        return {
            "security_flags": [_FLAG_ALL_COMPLETE],
            "current_step_id": None,
            "target_role": None,
        }

    # --- Bounded Failure ceiling ---
    # NOTE: retry_count is READ ONLY here. Increment is downstream's job
    # (validate_output / drift_monitor / future AnalystAgent). See module docstring.
    retry_count: int = int(state.get("retry_count", 0))
    if retry_count > MAX_RETRIES:
        logger.warning(
            "Orchestrator: step #%d exceeded MAX_RETRIES=%d — marking failed, "
            "escalating to HITL.",
            active.step_number,
            MAX_RETRIES,
        )
        updated_mission = _mark_step_status(mission, active.step_number, "failed")
        return {
            "mission_spec": updated_mission,
            "hitl_pending": True,
            "security_flags": [_FLAG_BOUNDED_FAILURE],
            "errors": [
                f"Orchestrator: step {active.step_number} ({active.target_role}) "
                f"failed after {retry_count} retries (cap={MAX_RETRIES})."
            ],
            "retry_count": 0,
            "current_step_id": active.step_number,
            "target_role": active.target_role,
        }

    # --- RED ALERT flag (informational; topology routing belongs to IntentRouter) ---
    flags: List[str] = []
    css_total: float = _safe_get_css(
        state.get("context_metrics"), float(state.get("css", 100.0))
    )
    if css_total < _RED_ALERT_CSS:
        flags.append(_FLAG_RED_ALERT)
        logger.warning(
            "Orchestrator: RED ALERT (CSS=%.1f < %.1f) — flag emitted for downstream HITL.",
            css_total,
            _RED_ALERT_CSS,
        )

    # --- Idempotent re-dispatch: step already in_progress (retry path) ---
    if active.status == "in_progress":
        logger.info(
            "Orchestrator: re-dispatching step #%d already in_progress "
            "(role=%s, retry_count=%d) — skipping mission mutation.",
            active.step_number,
            active.target_role,
            retry_count,
        )
        result: Dict[str, Any] = {
            "current_step_id": active.step_number,
            "target_role": active.target_role,
        }
        if flags:
            result["security_flags"] = flags
        return result

    # --- Happy path: first dispatch of this step ---
    updated_mission = _mark_step_status(mission, active.step_number, "in_progress")
    logger.info(
        "Orchestrator: dispatching step #%d (role=%s, action=%s, file=%s).",
        active.step_number,
        active.target_role,
        active.action,
        active.target_file,
    )
    result = {
        "mission_spec": updated_mission,
        "current_step_id": active.step_number,
        "target_role": active.target_role,
    }
    if flags:
        result["security_flags"] = flags
    return result
