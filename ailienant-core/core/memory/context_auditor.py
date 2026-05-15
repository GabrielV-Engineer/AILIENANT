# core/memory/context_auditor.py
"""Phase 3.3 — Context Auditor: Mini-Judge classifier and routing-tier derivation.

audit_task_complexity   — async, cheap LLM binary COMPLEX/SIMPLE verdict.
derive_routing_decision — pure-function 3-tier mapping (no I/O).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from shared.config import MINI_JUDGE_MODEL

logger = logging.getLogger("CONTEXT_AUDITOR")


class RiskLevel(str, Enum):
    """Semantic risk tiers emitted by the Mini-Judge.

    Ordering matters for the Veto Authority in agents/planner.py:
        NONE   — defer to mathematical routing.
        MEDIUM — force at least LOCAL_BIG.
        HIGH   — absolute veto → CLOUD.
    """
    NONE = "NONE"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_MINI_JUDGE_SYSTEM: str = (
    "Audit the user's coding task for semantic risk. Classify as one of:\n"
    "  HIGH   — multi-file refactoring impacts, deep AST mutations (class "
    "hierarchies, decorators, or core signatures), or logical complexity "
    "that outstrips local context availability.\n"
    "  MEDIUM — single-module refactor, non-trivial logic changes, or "
    "moderate scope touching more than a few functions.\n"
    "  NONE   — queries, explanations, regex fixes, or minor isolated edits.\n"
    "Respond with exactly one word: HIGH, MEDIUM, or NONE."
)


async def audit_task_complexity(user_input: str, session_id: str = "") -> RiskLevel:
    """Return the semantic RiskLevel for user_input.

    Uses MINI_JUDGE_MODEL for fast 3-state classification.
    Returns RiskLevel.NONE on empty input or any LLM failure
    (fail-safe default: don't escalate).
    LLMGateway is deferred to avoid circular imports at module load time.
    """
    if not user_input.strip():
        return RiskLevel.NONE
    try:
        from tools.llm_gateway import LLMGateway  # deferred — avoids circular at module level
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _MINI_JUDGE_SYSTEM},
                {"role": "user", "content": user_input},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            max_tokens=8,
            session_id=session_id,
        )
        raw: Optional[str] = response.choices[0].message.content
        verdict = (raw or "").strip().upper()
        if verdict.startswith("HIGH"):
            risk = RiskLevel.HIGH
        elif verdict.startswith("MEDIUM"):
            risk = RiskLevel.MEDIUM
        else:
            risk = RiskLevel.NONE
        logger.info(
            "MiniJudge: input_len=%d verdict=%r risk=%s",
            len(user_input), verdict, risk.value,
        )
        return risk
    except Exception as exc:
        logger.warning("MiniJudge: LLM call failed (non-fatal, defaulting NONE): %s", exc)
        return RiskLevel.NONE


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
