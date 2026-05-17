"""
brain/summarizer.py — StateSummarizer node (Memory Compactor).

Runs at the start of every LangGraph turn. If total message tokens exceed
THRESHOLD_RATIO of context_window, compresses old messages using MODEL_SMALL.

Preservation: keeps last KEEP_LAST_N messages (Cognitive Horizon) verbatim.
Condensation: remaining messages summarized in one LLM call to MODEL_SMALL.
Replacement: compressed view written back via _merge_messages __replace__ sentinel.
Resilience: on LLM failure, logs warning and truncates (keeps last KEEP_LAST_N).
"""
from __future__ import annotations

import logging
from typing import List, Dict

from tools.llm_gateway import LLMGateway
from tools.token_counter import PrecisionTokenCounter
from shared.config import MODEL_SMALL
from core.resource_manager import ResourceBroker  # Phase 2.27

logger = logging.getLogger("STATE_SUMMARIZER")

KEEP_LAST_N: int = 5
THRESHOLD_RATIO: float = 0.80

_PROMPT = (
    "Summarize the following technical conversation concisely, "
    "preserving key architectural decisions, variable names, and unresolved issues. "
    "Output only the summary text:\n\n{history}"
)


async def run_summarize_node(state: dict) -> dict:
    """LangGraph node: compress message history if token budget is exceeded.

    Returns {} (no-op) when history is within budget or too short to compress.
    Returns {"messages": [__replace__ sentinel, summary_msg, *recent]} on compression.
    """
    messages: List[Dict[str, str]] = state.get("messages", [])
    if len(messages) <= KEEP_LAST_N:
        return {}

    profile = state.get("active_llm_profile")
    context_window: int = profile.context_window if profile else 8192
    model_name: str = profile.model_name if profile else "gpt-4"
    threshold = int(context_window * THRESHOLD_RATIO)

    all_text = "\n".join(m.get("content", "") for m in messages if isinstance(m, dict))
    if PrecisionTokenCounter.estimate_with_buffer(all_text, model_name) <= threshold:
        return {}

    recent = messages[-KEEP_LAST_N:]
    to_summarize = messages[:-KEEP_LAST_N]
    history_text = "\n".join(
        f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in to_summarize
    )

    # Phase 2.27 — pre-flight VRAM contention check. SWITCH_TO_CLOUD swaps to
    # MODEL_BIG transparently. CANCEL falls back to plain truncation.
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)
    if decision.cancelled:
        logger.info(
            "StateSummarizer: cancelled by user during VRAM contention — truncating to last %d.",
            KEEP_LAST_N,
        )
        return {"messages": [{"__replace__": True}, *recent]}

    try:
        # Lock-held region: LLM call + content extraction. Inner finally guarantees
        # release even if response.choices indexing or attribute access raises.
        try:
            response = await LLMGateway.ainvoke(
                messages=[{"role": "user", "content": _PROMPT.format(history=history_text)}],
                model=decision.effective_model,
                temperature=0.0,
                max_tokens=512,
                session_id=state.get("task_id", ""),
            )
            summary_text = response.choices[0].message.content or "[Summary unavailable]"
        finally:
            if decision.holds_lock:
                await ResourceBroker.release(state.get("task_id", ""))

        logger.info(
            "StateSummarizer: compressed %d messages → 1 summary + %d recent (task=%s)",
            len(to_summarize), KEEP_LAST_N, state.get("task_id", ""),
        )
        return {
            "messages": [
                {"__replace__": True},
                {"role": "system", "content": f"[HISTORY SUMMARY]: {summary_text}"},
                *recent,
            ]
        }
    except Exception as exc:
        logger.warning(
            "StateSummarizer: LLM call failed (%s) — truncating to last %d messages.",
            exc, KEEP_LAST_N,
        )
        return {"messages": [{"__replace__": True}, *recent]}
