# core/memory/context_auditor.py
"""Phase 3.3 — Context Auditor: Mini-Judge classifier and routing-tier derivation.

audit_task_complexity   — async, cheap LLM binary COMPLEX/SIMPLE verdict.
derive_routing_decision — pure-function 3-tier mapping (no I/O).
"""
from __future__ import annotations

import logging
from typing import Optional

from shared.config import MINI_JUDGE_MODEL

logger = logging.getLogger("CONTEXT_AUDITOR")

_MINI_JUDGE_SYSTEM: str = (
    "Determine if the user's coding task is COMPLEX (requires refactoring, "
    "architecture changes, or deep optimization) or SIMPLE (queries, explanations, "
    "regex fixes, or minor edits). Respond with exactly one word: COMPLEX or SIMPLE."
)


async def audit_task_complexity(user_input: str, session_id: str = "") -> bool:
    """Return True if user_input describes a COMPLEX coding task.

    Uses MINI_JUDGE_MODEL for fast binary classification.
    Returns False on empty input or any LLM failure (fail-safe default: don't escalate).
    LLMGateway is deferred to avoid circular imports at module load time.
    """
    if not user_input.strip():
        return False
    try:
        from tools.llm_gateway import LLMGateway  # deferred — avoids circular at module level
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _MINI_JUDGE_SYSTEM},
                {"role": "user", "content": user_input},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            max_tokens=5,
            session_id=session_id,
        )
        raw: Optional[str] = response.choices[0].message.content
        verdict = (raw or "").strip().upper()
        is_complex = verdict.startswith("COMPLEX")
        logger.info(
            "MiniJudge: input_len=%d verdict=%r complex=%s",
            len(user_input), verdict, is_complex,
        )
        return is_complex
    except Exception as exc:
        logger.warning("MiniJudge: LLM call failed (non-fatal, defaulting False): %s", exc)
        return False


def derive_routing_decision(tci: float, css: float) -> str:
    """Map TCI + CSS to ContextMeter routing_decision tier string.

    Thresholds align with RoutingEngine CSS/TCI matrix (brain/routing_engine.py):
        css < 40          → CLOUD       (red-alert: maximum context needed)
        tci < 30          → LOCAL_SMALL (simple task, privacy-first)
        30 ≤ tci < 75     → LOCAL_BIG   (medium complexity)
        tci ≥ 75          → CLOUD       (cognitively demanding)
    """
    if css < 40.0:
        return "CLOUD"
    if tci < 30.0:
        return "LOCAL_SMALL"
    if tci < 75.0:
        return "LOCAL_BIG"
    return "CLOUD"
