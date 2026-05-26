# ailienant-core/agents/inline_edit.py
#
# Phase 7.11.1 (ADR-706 §4.5a) — Streaming inline-edit agent for Cmd+K mutations.
#
# Contract:
#   stream_inline_edit yields typed deltas the frontend InlineMutationManager
#   replays into the active editor:
#     1. One upfront {"kind":"DELETE","offset":<sel_start>,"length":<sel_len>,"text":""}
#        — clears the user's selection range.
#     2. A series of {"kind":"INSERT","offset":<sel_start>,"length":0,"text":<chunk>}
#        progressively appending the LLM's replacement text at the deletion point.
#     3. On validation failure mid-stream: one
#        {"kind":"ABORT","offset":0,"length":0,"text":"<reason>"} then stop.
#
# Cancellation (plan W2 — no orphaned tokens):
#   - The orchestrator passes a `cancel_event: asyncio.Event`. Every loop turn
#     checks it BEFORE yielding; if set, emits ABORT and returns.
#   - `asyncio.CancelledError` raised by `task.cancel()` is caught around the
#     LLM stream, the generator emits ABORT and re-raises so litellm's async
#     iterator can free its upstream HTTP slot.
#
# Cognitive isolation: MUST NOT import brain.personality.
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from tools.inline_patch_validator import validate_partial_syntax
from tools.llm_gateway import LLMGateway
from transport.token_batcher import batch_tokens

logger = logging.getLogger("INLINE_EDIT")

# The LLM is steered to emit ONLY the replacement code, no fences, no prose.
# A short, durable system prompt — kept here (not in shared/persona) precisely
# because this agent sits behind the cognitive-isolation fence.
_INLINE_EDIT_SYSTEM_PROMPT: str = (
    "You are an inline code editor invoked by Cmd+K. The user has selected a "
    "region of code and given a single instruction. Output ONLY the replacement "
    "text for that region — no markdown fences, no commentary, no XML, no "
    "language tags. Preserve the surrounding indentation level. Match the file's "
    "existing style (quotes, semicolons, naming). If the instruction asks for "
    "removal, output an empty string. Stop when the replacement is complete."
)


def _user_prompt(
    instruction: str,
    file_path: str,
    selected_text: str,
    language_id: Optional[str],
) -> str:
    lang_tag = language_id or "text"
    return (
        f"File: {file_path}\n"
        f"Language: {lang_tag}\n\n"
        f"Selected region:\n---\n{selected_text}\n---\n\n"
        f"Instruction: {instruction}\n\n"
        f"Output the replacement for the selected region only."
    )


async def stream_inline_edit(
    prompt: str,
    file_path: str,
    file_content: str,
    selection_range: Tuple[int, int],
    language_id: Optional[str],
    *,
    session_id: str,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Stream typed deltas (DELETE, INSERT…, ABORT?) for an inline edit.

    Validates the speculative buffer after every coalesced LLM chunk via
    `validate_partial_syntax`; on a hard rejection, yields an ABORT and stops
    cleanly. Honors `cancel_event` between yields and propagates
    `asyncio.CancelledError` so the upstream LiteLLM connection is released.
    """
    sel_start, sel_end = selection_range
    if sel_start < 0:
        sel_start = 0
    if sel_end < sel_start:
        sel_end = sel_start
    sel_end = min(sel_end, len(file_content))
    selected_text = file_content[sel_start:sel_end]
    selection_length = sel_end - sel_start

    # 1) Upfront DELETE — the manager clears the selection range so subsequent
    # INSERTs land at the deletion point (sel_start).
    initial_delete: Dict[str, Any] = {
        "kind": "DELETE",
        "offset": sel_start,
        "length": selection_length,
        "text": "",
    }
    yield initial_delete

    deltas_so_far: list[Dict[str, Any]] = [initial_delete]

    if cancel_event is not None and cancel_event.is_set():
        yield {"kind": "ABORT", "offset": 0, "length": 0, "text": "user_cancel"}
        return

    messages = [
        {"role": "system", "content": _INLINE_EDIT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _user_prompt(prompt, file_path, selected_text, language_id),
        },
    ]

    insert_cursor = sel_start  # absolute LF-space offset where the next chunk lands
    raw_stream = None
    try:
        raw_stream = LLMGateway.astream_byom(
            messages, tier="medium", session_id=session_id
        )
        async for chunk in batch_tokens(raw_stream, chunk_ms=40):
            if cancel_event is not None and cancel_event.is_set():
                yield {"kind": "ABORT", "offset": 0, "length": 0, "text": "user_cancel"}
                return
            if not chunk:
                continue
            candidate: Dict[str, Any] = {
                "kind": "INSERT",
                "offset": insert_cursor,
                "length": 0,
                "text": chunk,
            }
            speculative = deltas_so_far + [candidate]
            if not validate_partial_syntax(
                file_path, file_content, speculative, language_id=language_id
            ):
                logger.info(
                    "INLINE_EDIT: hard syntax rejection at offset=%d (chunk_len=%d)",
                    insert_cursor, len(chunk),
                )
                yield {
                    "kind": "ABORT",
                    "offset": insert_cursor,
                    "length": 0,
                    "text": "syntax_break",
                }
                return
            deltas_so_far.append(candidate)
            insert_cursor += len(chunk)
            yield candidate
    except asyncio.CancelledError:
        # Cooperative cancellation — emit one ABORT and re-raise so the task
        # is marked cancelled and the litellm async iterator can close.
        yield {"kind": "ABORT", "offset": 0, "length": 0, "text": "user_cancel"}
        raise
    except Exception as exc:  # noqa: BLE001 — must never crash the WS loop
        logger.warning("INLINE_EDIT: stream failed: %s", exc)
        yield {"kind": "ABORT", "offset": 0, "length": 0, "text": f"error: {exc}"}
        return
    finally:
        # Best-effort close on the underlying iterator so LiteLLM frees its
        # upstream HTTP slot if cancellation interrupted the stream.
        close = getattr(raw_stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — close is best-effort
                pass
