"""
brain/guardrails.py — Output Validation & Self-Correction Loop (Phase 2.1.14).

Sits between coder_agent and END in the LangGraph topology. Validates state
against the CoderOutput Pydantic schema.

On failure:  increments retry_count, sets validation_feedback for CoderAgent re-try.
On success:  clears guardrail_failed and validation_feedback.
At max retries (MAX_RETRIES=2): clears guardrail_failed and routes to END gracefully.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, ValidationError
from langgraph.graph import END

from brain.retry_policy import GUARDRAIL_MAX_RETRIES

logger = logging.getLogger("OUTPUT_GUARDRAIL")

MAX_RETRIES: int = GUARDRAIL_MAX_RETRIES
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class CoderOutput(BaseModel):
    """Phase 2.1.14 stub schema — expanded with full field contracts in Phase 4."""

    vfs_buffer: Dict[str, Any]
    current_step_id: Optional[int] = None
    target_role: Optional[str] = None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Regex fallback: extract JSON from fenced code blocks before Pydantic."""
    m = _JSON_FENCE_RE.search(text)
    try:
        result = json.loads(m.group(1) if m else text)
        return dict(result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, AttributeError):
        return None


async def run_validate_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: validate CoderAgent output against CoderOutput schema.

    Returns {} (pass) when validation succeeds.
    Returns error dict with guardrail_failed=True on failure (up to MAX_RETRIES).
    Returns guardrail_failed=False after MAX_RETRIES to allow graceful END.
    """
    retry_count: int = state.get("retry_count", 0)
    output = {
        "vfs_buffer": state.get("vfs_buffer", {}),
        "current_step_id": state.get("current_step_id"),
        "target_role": state.get("target_role"),
    }
    try:
        CoderOutput(**output)
        # ── Phase 3.0.2: Fire-and-forget trajectory persistence ──────────────
        # SPOF guard: any failure here (network, LiteLL proxy, disk) MUST NOT
        # abort a mission that has already validated successfully.
        try:
            from core.memory.trajectory_memory import TrajectoryMemoryManager
            await TrajectoryMemoryManager().memorize_trajectory(state, success_flag=True)
        except Exception as _traj_err:
            logger.warning(
                "TrajectoryMemory: memorize failed (non-fatal, mission unaffected): %s",
                _traj_err,
            )
        # ────────────────────────────────────────────────────────────────────
        return {"guardrail_failed": False, "validation_feedback": None}
    except ValidationError as exc:
        new_retry = retry_count + 1
        if new_retry > MAX_RETRIES:
            logger.warning(
                "Guardrail: max retries (%d) exhausted — failing gracefully.", MAX_RETRIES
            )
            return {"guardrail_failed": False, "validation_feedback": None}
        feedback = (
            f"[GUARDRAIL ERROR — attempt {new_retry}/{MAX_RETRIES}]: {exc}. "
            "Correct your response and try again."
        )
        logger.warning("Guardrail failed (retry %d/%d): %s", new_retry, MAX_RETRIES, exc)
        return {
            "guardrail_failed": True,
            "validation_feedback": feedback,
            "retry_count": new_retry,
        }


def route_after_validation(state: Dict[str, Any]) -> str:
    """Conditional edge: retry the step, advance to the next WBS step, or END.

    - ``guardrail_failed`` → retry the SAME step (bounded by the guardrail node's
      MAX_RETRIES, which clears the flag on exhaustion).
    - else, if the WBS still has a ``pending`` step → loop back to ``drift_gate``,
      which re-runs ``route_to_coders`` to dispatch the next step (and re-traverses
      ``finops_gate``/``supervisor_node`` so the budget ceiling is re-checked each
      iteration). This is the RELAY multi-step loop.
    - else → END (all steps reached a terminal status).

    Uses the SAME ``is_dispatchable`` predicate ``route_to_coders`` selects on
    (``brain/state.py``), so routing and dispatch never disagree — widened
    alongside it for 13.0.9's incremental apply gate (``revision_requested`` is
    dispatchable-again, not a stall). A stall guard ends the graph if the step
    just executed reached neither a terminal status NOR ``revision_requested``
    (a durable-write failure, an early-return step, or — new in 13.0.9 — a step
    that reached ``apply_patch`` still ``in_progress`` with no artifact to
    apply, since orphaning it there would otherwise defeat this exact guard)
    so a non-advancing loop can never spin.

    Also routes ``healing_required`` to ``error_correction`` (13.0.9): once
    ``run_command`` execution moves downstream of the coder into the apply
    gate, a non-zero-exit self-heal signal has nowhere else to route from.
    """
    from brain.state import is_dispatchable, is_terminal
    from core.telemetry import log_routing_decision

    if state.get("guardrail_failed"):
        log_routing_decision(
            session_id=state.get("task_id", ""),
            source="validate_output",
            target="coder_agent",
            reason=f"guardrail_failed=True (retry {state.get('retry_count', 0)}/{MAX_RETRIES})",
        )
        return "coder_agent"

    if state.get("healing_required"):
        log_routing_decision(
            session_id=state.get("task_id", ""),
            source="validate_output",
            target="error_correction",
            reason="healing_required=True",
        )
        return "error_correction"

    mission = state.get("mission_spec")
    tasks = list(getattr(mission, "tasks", []) or [])
    current = state.get("current_step_id")

    # Stall guard: the step we just ran MUST have reached a terminal status, or
    # be legitimately re-dispatchable (revision_requested). Anything else —
    # still pending, or orphaned in_progress/awaiting_approval — means looping
    # would re-dispatch nothing or nobody forever; end instead.
    just_ran = next((t for t in tasks if t.step_number == current), None)
    if just_ran is not None and not is_terminal(just_ran) and just_ran.status != "revision_requested":
        logger.error(
            "route_after_validation: step #%s in non-advancing status %r after "
            "execution — ending to avoid a non-advancing loop.",
            current, just_ran.status,
        )
        _log_end(state, "wbs_stall_step_not_terminal")
        return END

    if any(is_dispatchable(t) for t in tasks):
        log_routing_decision(
            session_id=state.get("task_id", ""),
            source="validate_output",
            target="drift_gate",
            reason="wbs_advance_next_pending_step",
        )
        return "drift_gate"

    _log_end(state, "wbs_all_steps_terminal")
    return END


def _log_end(state: Dict[str, Any], reason: str) -> None:
    from core.telemetry import log_routing_decision
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="validate_output",
        target="__end__",
        reason=reason,
    )
