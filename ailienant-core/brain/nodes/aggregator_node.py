"""Phase 3.4.2 — Session Delta Aggregator (Pre-Dream Reflection LangGraph node).

Reads messages, terminal_output, and vfs_buffer from the graph state and
produces a compact session_delta string (≤500 tokens) before each planner turn.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, cast

logger = logging.getLogger("SESSION_DELTA")

_MAX_TOKENS: int = 500
_MAX_CHARS: int = _MAX_TOKENS * 4  # ≈4 chars per token


async def run_session_delta_aggregator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: compile a compact session_delta from messages, terminal_output, vfs_buffer."""
    messages: List[Dict[str, str]] = cast(List[Dict[str, str]], state.get("messages", []))
    terminal_output: str = cast(str, state.get("terminal_output", ""))
    vfs_buffer: Dict[str, Any] = cast(Dict[str, Any], state.get("vfs_buffer", {}))
    task_id: str = cast(str, state.get("task_id", ""))

    intent = await _extract_intent(messages, task_id)
    blockers = _extract_blockers(terminal_output)
    context_state = _extract_context_state(vfs_buffer)

    delta = _format_delta(intent, blockers, context_state)
    logger.info("session_delta built (%d chars) for task %s", len(delta), task_id)
    return {"session_delta": delta}


async def _extract_intent(messages: List[Dict[str, str]], task_id: str) -> str:
    """Return a 1-sentence intent summary via LLM, falling back to last user message."""
    user_messages = [m["content"] for m in messages if m.get("role") == "user"]
    recent = user_messages[-5:]
    if not recent:
        return "No prior user intent recorded."
    try:
        from agents.analyst import generate_intent_summary_llm  # deferred — avoids circular import
        return await generate_intent_summary_llm(recent, task_id)
    except Exception as exc:
        logger.warning("Intent LLM failed (%s), using deterministic fallback.", exc)
        return recent[-1][:200]


def _extract_blockers(terminal_output: str) -> str:
    """Scan terminal_output for error/warning lines; return compact summary."""
    if not terminal_output.strip():
        return "None."
    lines = terminal_output.splitlines()
    error_lines = [
        ln.strip() for ln in lines
        if any(kw in ln.lower() for kw in ("error", "exception", "traceback", "failed", "warning"))
    ]
    if not error_lines:
        return "None."
    unique = list(dict.fromkeys(error_lines))
    summary = "; ".join(unique[:5])
    return summary[:300]


def _extract_context_state(vfs_buffer: Dict[str, Any]) -> str:
    """List dirty (staged but unapplied) file paths from vfs_buffer."""
    if not vfs_buffer:
        return "No staged files."
    paths = sorted(vfs_buffer.keys())
    listed = ", ".join(paths[:10])
    suffix = f" (+{len(paths) - 10} more)" if len(paths) > 10 else ""
    return listed + suffix


def _format_delta(intent: str, blockers: str, context_state: str) -> str:
    """Assemble the fixed-template session_delta string, capped at _MAX_CHARS."""
    raw = (
        "### SESSION DELTA\n"
        f"- INTENT: {intent}\n"
        f"- BLOCKERS: {blockers}\n"
        f"- CONTEXT_STATE: {context_state}"
    )
    if len(raw) > _MAX_CHARS:
        raw = raw[:_MAX_CHARS - 3] + "..."
    return raw
