"""
brain/summarizer.py — StateSummarizer node (Memory Compactor).

Runs at the start of every LangGraph turn. If total message tokens exceed
THRESHOLD_RATIO of context_window, compresses old messages using MODEL_SMALL.

Preservation: keeps last KEEP_LAST_N messages (Cognitive Horizon) verbatim.
Condensation: remaining messages summarized in one LLM call to MODEL_SMALL.
Replacement: compressed view written back via _merge_messages __replace__ sentinel.
Resilience: on LLM failure, logs warning and truncates (keeps last KEEP_LAST_N).
STATE_COMPACTED: after any compression, fires the on_state_compacted callback
                 (if present in config.configurable) so the IDE can render a
                 compaction notice. The callback is fire-and-forget — a dead
                 WebSocket never aborts the summarizer node.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from tools.llm_gateway import LLMGateway
from tools.token_counter import PrecisionTokenCounter
from shared.config import MODEL_SMALL
from core.resource_manager import ResourceBroker

logger = logging.getLogger("STATE_SUMMARIZER")

KEEP_LAST_N: int = 5
THRESHOLD_RATIO: float = 0.80

_PROMPT = (
    "Summarize the following technical conversation concisely, "
    "preserving key architectural decisions, variable names, and unresolved issues. "
    "Output only the summary text:\n\n{history}"
)


async def _emit_compacted(config: Optional[RunnableConfig], message: str, n: int) -> None:
    """Fire the STATE_COMPACTED notification callback. Never raises.

    A dead WebSocket or transport error must not abort the summarizer node or corrupt
    LangGraph state — UI telemetry is low-criticality and loss is acceptable.
    Wire via: functools.partial(vfs_manager.broadcast_state_compacted, session_id)
    bound as config["configurable"]["on_state_compacted"].
    """
    cfg = (config or {}).get("configurable") or {}
    cb = cfg.get("on_state_compacted")
    if cb is not None:
        try:
            await cb(message, n)
        except Exception:  # noqa: BLE001
            logger.warning(
                "STATE_COMPACTED notification failed (dead WS or transport error); "
                "summarizer state unaffected.",
                exc_info=True,
            )


async def _run_summarize_node_core(
    state: Dict[str, Any],
    config: Optional[RunnableConfig] = None,
    *,
    _telemetry_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """LangGraph node core: compress message history if token budget is exceeded.

    Returns {} (no-op) when history is within budget or too short to compress.
    Returns {"messages": [__replace__ sentinel, summary_msg, *recent]} on compression.

    ``_telemetry_sink``, when provided, is populated with ``total_tokens``/
    ``token_budget``/``turn_count`` at the exact points this function already
    computes them for its own logic — a side channel for the public
    ``run_summarize_node`` wrapper's telemetry emission, so nothing is ever
    tokenized twice.
    """
    messages: List[Dict[str, str]] = state.get("messages", [])
    if _telemetry_sink is not None:
        _telemetry_sink["turn_count"] = len(messages)
    if len(messages) <= KEEP_LAST_N:
        return {}

    profile = state.get("active_llm_profile")
    context_window: int = profile.context_window if profile else 8192
    model_name: str = profile.model_name if profile else "gpt-4"
    threshold = int(context_window * THRESHOLD_RATIO)

    all_text = "\n".join(m.get("content", "") for m in messages if isinstance(m, dict))
    total_tokens = PrecisionTokenCounter.estimate_with_buffer(all_text, model_name)
    if _telemetry_sink is not None:
        _telemetry_sink["total_tokens"] = total_tokens
        _telemetry_sink["token_budget"] = context_window
    if total_tokens <= threshold:
        return {}

    recent = messages[-KEEP_LAST_N:]
    to_summarize = messages[:-KEEP_LAST_N]
    history_text = "\n".join(
        f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in to_summarize
    )

    # Pre-flight VRAM contention check. SWITCH_TO_CLOUD swaps to MODEL_BIG
    # transparently. CANCEL falls back to plain truncation (user-initiated — no
    # STATE_COMPACTED emitted, since the user caused the early exit, not the engine).
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
        await _emit_compacted(
            config,
            f"Compacted {len(to_summarize)} conversation turn(s) to fit the context window.",
            len(to_summarize),
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
        await _emit_compacted(
            config,
            f"Truncated to last {KEEP_LAST_N} conversation turn(s).",
            len(messages) - KEEP_LAST_N,
        )
        return {"messages": [{"__replace__": True}, *recent]}


def _emit_summarizer_telemetry(state: Dict[str, Any], sink: Dict[str, Any]) -> None:
    """Best-effort context-utilization telemetry sample. Never raises — a
    failure here must never mask the real return value or a real exception
    propagating out of run_summarize_node's finally block."""
    try:
        from core.telemetry_log import log_context_utilization
        start = state.get("session_start_time")
        log_context_utilization(
            session_id=str(state.get("task_id", "")), source="summarizer",
            total_tokens=sink.get("total_tokens", 0),
            token_budget=sink.get("token_budget", 0),
            turn_count=sink.get("turn_count", len(state.get("messages", []))),
            duration_s=(time.time() - start) if start else 0.0,
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        logger.debug("context-utilization telemetry emit failed", exc_info=True)


async def run_summarize_node(
    state: Dict[str, Any],
    config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    """LangGraph node: compress message history if token budget is exceeded.

    Delegates to _run_summarize_node_core, then emits a context-utilization
    telemetry sample as a side effect. The return value is always identical
    to calling _run_summarize_node_core directly — telemetry never observes
    or mutates it.
    """
    sink: Dict[str, Any] = {}
    try:
        return await _run_summarize_node_core(state, config, _telemetry_sink=sink)
    finally:
        _emit_summarizer_telemetry(state, sink)
