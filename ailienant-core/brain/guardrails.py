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

logger = logging.getLogger("OUTPUT_GUARDRAIL")

MAX_RETRIES: int = 2
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class CoderOutput(BaseModel):
    """Phase 2.1.14 stub schema — expanded with full field contracts in Phase 4."""

    vfs_buffer: Dict[str, Any]
    current_step_id: Optional[int] = None
    target_role: Optional[str] = None


def _extract_json(text: str) -> Optional[dict]:
    """Regex fallback: extract JSON from fenced code blocks before Pydantic."""
    m = _JSON_FENCE_RE.search(text)
    try:
        return json.loads(m.group(1) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return None


async def run_validate_output_node(state: dict) -> dict:
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


def route_after_validation(state: dict) -> str:
    """Conditional edge: retry CoderAgent or proceed to END."""
    from core.telemetry import log_routing_decision
    if state.get("guardrail_failed"):
        target = "coder_agent"
        reason = f"guardrail_failed=True (retry {state.get('retry_count', 0)}/{MAX_RETRIES})"
    else:
        target = "__end__"
        reason = "guardrail_passed"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="validate_output",
        target=target,
        reason=reason,
    )
    return "coder_agent" if state.get("guardrail_failed") else END
