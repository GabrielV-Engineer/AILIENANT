# ailienant-core/brain/fast_path.py
"""Phase 4.3 — SEQUENTIAL Mode: zero-LangGraph, zero-SQLite functional bypass.

Allowed tools: FileReadTool, GrepTool, query_graphrag, RunLinterTool (read-only).
Forbidden: apply_patch, BashTool, BatchEditTool.
Single LLM turn. No retry loop. No checkpoint persisted.
"""
import logging
from typing import Any, Dict

logger = logging.getLogger("FAST_PATH")

_SEQUENTIAL_ALLOWED_TOOLS = frozenset([
    "FileReadTool", "GrepTool", "query_graphrag", "RunLinterTool",
])


async def execute_sequential_bypass(
    prompt: str,
    workspace_root: str,
    task_id: str = "",
) -> Dict[str, Any]:
    """Single-turn Analyst pipe. Returns graph-shape dict; no LangGraph involved.

    Return shape: {"messages": list[dict], "shared_understanding_reached": bool}
    Mirrors run_analyst_node() output so task_service / websocket_manager can call
    broadcast_token(task_id, content) without modification.
    """
    from brain.personality import soul_manager  # Analyst path — §3.4 fence permits this
    from tools.llm_gateway import LLMGateway  # noqa: E402 — deferred: avoids circular import
    from shared.config import MODEL_SMALL  # noqa: E402

    soul_prompt: str = soul_manager.get_prompt()
    logger.info(
        "FastPath[SEQUENTIAL]: soul_prompt=%d chars task_id=%s",
        len(soul_prompt), task_id,
    )

    answer: str
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": soul_prompt},
                {"role": "user",   "content": prompt},
            ],
            model=MODEL_SMALL,
            temperature=0.2,
            max_tokens=512,
            session_id=task_id,
        )
        raw: str | None = response.choices[0].message.content
        answer = raw if raw is not None else "[SEQUENTIAL] Empty LLM response."
        logger.info("FastPath[SEQUENTIAL]: response received (%d chars).", len(answer))
    except Exception as exc:
        answer = f"[SEQUENTIAL STUB] Analyst echo: {prompt[:120]}"
        logger.warning("FastPath[SEQUENTIAL]: LLM unavailable (%s) — echo stub.", exc)

    return {
        "messages": [
            {"role": "user",      "content": prompt},
            {"role": "assistant", "content": answer},
        ],
        "shared_understanding_reached": True,
    }
