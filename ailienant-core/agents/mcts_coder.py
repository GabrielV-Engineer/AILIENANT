# agents/mcts_coder.py
"""Phase 3.4.8 — MCTS-side Hybrid CoderAgent.

Standalone helpers that the MCTS Overnight Daemon (Phase 3.4.3b) will call
during node expansion / reward evaluation. NOT a LangGraph node —
`agents/coder.py::run_coder_node` stays untouched for the Phase 4 LangGraph
flow.

Architecture:
    - generate_local_variant      — Tier.LOCAL code generation
    - local_fix_with_retry        — validate_delta + up to 3 Tier.LOCAL repair attempts
    - surgeon_escalation          — Tier.CLOUD repair when error_streak hits 3
    - evaluate_node_reward        — full orchestrator (local → maybe surgeon → judge or -1.0)

Every LLM call automatically lands in TokenLedger via tools/llm_gateway.py.
"""
from __future__ import annotations

import logging
from typing import Any, Tuple

from agents.analyst import supreme_judge_evaluate
from brain.mcts.tree import MCTSNode
from tools.llm_gateway import LLMGateway, Tier
from tools.validation.pipeline import validate_delta
from tools.validation.result import PipelineResult

logger = logging.getLogger("MCTS_CODER")

MAX_LOCAL_ATTEMPTS: int = 3
_FAILED_REWARD: float = -1.0

_LOCAL_GENERATE_SYSTEM: str = (
    "You are a code generator. Output ONLY the code, no prose, no markdown fences."
)
_LOCAL_FIX_SYSTEM: str = (
    "You are a code-fixer. You will receive a snippet and a static-analysis error. "
    "Produce a corrected version that fixes the specific error. "
    "Output ONLY the corrected code, no prose, no markdown fences."
)
_SURGEON_SYSTEM: str = (
    "You are an expert code surgeon. The local model has failed 3 consecutive times "
    "to fix this code. Diagnose the root cause from the error trace, then produce a "
    "corrected version. Output ONLY the corrected code, no prose, no markdown fences."
)


def _extract_content(response: Any) -> str:
    """Defensively pull `.choices[0].message.content` from a ModelResponse."""
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise ValueError(f"Unexpected LLM response shape: {exc}") from exc
    if content is None:
        return ""
    return str(content)


async def generate_local_variant(
    content: str,
    file_path: str,
    session_id: str = "",
) -> str:
    """Phase 3.4.8 — generate a new code variant via Tier.LOCAL.

    Used by the MCTS daemon during node expansion to propose a new dreamed
    state without touching the cloud.
    """
    response = await LLMGateway.ainvoke(
        messages=[
            {"role": "system", "content": _LOCAL_GENERATE_SYSTEM},
            {"role": "user", "content": f"### File: {file_path}\n### Current content:\n{content}"},
        ],
        tier=Tier.LOCAL,
        temperature=0.2,
        max_tokens=2000,
        session_id=session_id,
    )
    return _extract_content(response)


async def _ask_local_to_fix(
    content: str,
    file_path: str,
    error: str,
    session_id: str,
) -> str:
    """Ask Tier.LOCAL to repair `content` based on `error`."""
    response = await LLMGateway.ainvoke(
        messages=[
            {"role": "system", "content": _LOCAL_FIX_SYSTEM},
            {"role": "user", "content": (
                f"### File: {file_path}\n"
                f"### Error: {error}\n"
                f"### Code:\n{content}"
            )},
        ],
        tier=Tier.LOCAL,
        temperature=0.0,
        max_tokens=2000,
        session_id=session_id,
    )
    return _extract_content(response)


async def local_fix_with_retry(
    initial_content: str,
    file_path: str,
    node: MCTSNode,
    session_id: str = "",
) -> Tuple[str, PipelineResult]:
    """Run validate_delta with up to MAX_LOCAL_ATTEMPTS Tier.LOCAL repair attempts.

    Mutates node.error_streak. Returns (final_content, final_pipeline_result).
    A passed=True result means the caller can proceed straight to the Supreme
    Judge; passed=False means the Circuit Breaker should escalate to Cloud.
    """
    content: str = initial_content
    result: PipelineResult = await validate_delta(content, file_path)
    if result.passed:
        node.error_streak = 0
        return content, result

    for attempt in range(MAX_LOCAL_ATTEMPTS):
        node.error_streak += 1
        last_error: str = result.prune_reason or "unknown error"
        logger.info(
            "Local fix attempt %d/%d on %s (error_streak=%d): %s",
            attempt + 1, MAX_LOCAL_ATTEMPTS, file_path, node.error_streak, last_error,
        )
        content = await _ask_local_to_fix(content, file_path, last_error, session_id)
        result = await validate_delta(content, file_path)
        if result.passed:
            node.error_streak = 0
            return content, result

    # Exhausted MAX_LOCAL_ATTEMPTS — caller should escalate.
    return content, result


async def surgeon_escalation(
    content: str,
    file_path: str,
    last_error: str,
    node: MCTSNode,
    session_id: str = "",
) -> str:
    """Phase 3.4.8 Circuit Breaker — escalate to Tier.CLOUD when error_streak >= 3.

    Resets node.error_streak to 0 IF the surgeon's output passes validation.
    """
    logger.info(
        "Surgeon escalation: node=%s.. error_streak=%d file=%s",
        node.node_id[:8], node.error_streak, file_path,
    )
    response = await LLMGateway.ainvoke(
        messages=[
            {"role": "system", "content": _SURGEON_SYSTEM},
            {"role": "user", "content": (
                f"### File: {file_path}\n"
                f"### Persistent error (after 3 local attempts):\n{last_error}\n"
                f"### Current code:\n{content}"
            )},
        ],
        tier=Tier.CLOUD,
        temperature=0.0,
        max_tokens=4000,
        session_id=session_id,
    )
    fixed: str = _extract_content(response)
    post_check: PipelineResult = await validate_delta(fixed, file_path)
    if post_check.passed:
        node.error_streak = 0
        logger.info("Surgeon succeeded; error_streak reset to 0.")
    return fixed


async def evaluate_node_reward(
    content: str,
    file_path: str,
    workspace_root: str,
    node: MCTSNode,
    session_id: str = "",
) -> float:
    """Phase 3.4.8 — full orchestrator producing the MCTS reward in [-1.0, 1.0].

    Flow:
        1. local_fix_with_retry  (Tier.LOCAL, up to 3 attempts)
        2. If still failing      → surgeon_escalation (Tier.CLOUD), then re-validate.
        3. If STILL failing      → return _FAILED_REWARD (-1.0). NO Supreme Judge call.
        4. Otherwise             → supreme_judge_evaluate (Tier.CLOUD) → return reward [0, 1].
    """
    fixed_content, result = await local_fix_with_retry(content, file_path, node, session_id)

    if not result.passed:
        fixed_content = await surgeon_escalation(
            fixed_content,
            file_path,
            result.prune_reason or "unknown error",
            node,
            session_id,
        )
        result = await validate_delta(fixed_content, file_path)

    if not result.passed:
        logger.info(
            "Node %s.. unresolvable after surgeon; reward=%.1f",
            node.node_id[:8], _FAILED_REWARD,
        )
        return _FAILED_REWARD

    evaluation = await supreme_judge_evaluate(
        fixed_content, workspace_root, session_id=session_id,
    )
    return evaluation.reward
