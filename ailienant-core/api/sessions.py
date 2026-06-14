"""
api/sessions.py — Phase 7.11.8 (ADR-706 §4.5g) Time-Travel Debugging.

A thin read-only router that surfaces the chain of L2-promoted checkpoints
for a given session/thread. The frontend's CheckpointPicker overlay calls
``GET /api/v1/sessions/{thread_id}/checkpoints`` to populate the time-travel
list; per-message inline branch buttons get the checkpoint_id via the WS
``server_stream_end`` payload and never hit this REST endpoint.

Why REST instead of a WS event for *listing*:
  * The list is a snapshot — there is no streaming or per-frame interest in
    every checkpoint write. A one-shot GET avoids polluting the live WS
    channel with bulk data.
  * It can be opened in DevTools / curl for forensic / smoke debugging.
  * It mirrors the shape of ``api/audit.py`` exactly (one router, one path,
    one Pydantic response model).

Auth posture matches the rest of ``api/``: the backend assumes loopback +
trusted client; the data is read-only and per-session-scoped. The payload
itself surfaces only opaque IDs + timestamps + `termination_reason` — no
serialized state, no model output. See plan W10 (cybersecurity posture).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, cast

from fastapi import APIRouter
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from brain.checkpoint import checkpoint_manager

logger = logging.getLogger(__name__)

# Conservative window used only when the live state carries no profile yet
# (cold thread, pre-orchestrator). Mirrors the circuit-breaker default so the
# meter reads sanely before the first routing decision sets a real profile.
_DEFAULT_CONTEXT_WINDOW = 200_000

router = APIRouter(prefix="/api/v1/sessions", tags=["time-travel"])


class CheckpointEntry(BaseModel):
    """One node of the per-thread checkpoint chain."""

    checkpoint_id: str
    parent_id: Optional[str]
    promoted_at: float
    termination_reason: Optional[str]
    turn_index: int


@router.get("/{thread_id}/checkpoints", response_model=List[CheckpointEntry])
async def list_session_checkpoints(thread_id: str) -> List[CheckpointEntry]:
    """Return the chronological chain of L2 checkpoints for ``thread_id``.

    Empty list when the thread is unknown, the L2 connection isn't bound
    (test rig before lifespan), or no checkpoints have been promoted yet.
    Never raises — the caller's worst case is an empty picker UI.
    """
    rows = checkpoint_manager.list_checkpoints(thread_id)
    return [
        CheckpointEntry(
            checkpoint_id=r.checkpoint_id,
            parent_id=r.parent_id,
            promoted_at=r.promoted_at,
            termination_reason=r.termination_reason,
            turn_index=i,
        )
        for i, r in enumerate(rows)
    ]


class ContextOccupancy(BaseModel):
    """Live context-window occupancy for one thread.

    ``context_used_tokens`` is the token length of the *current* LangGraph
    message window — the pruned/summarized array the model will actually see —
    NOT a cumulative ledger total. It therefore goes DOWN after the summarizer
    compresses old turns, which is the whole point: the meter must reflect real
    window pressure, not lifetime usage.
    """

    context_window: int
    context_used_tokens: int
    context_pct: float


def _serialize_messages_for_count(messages: object) -> str:
    """Flatten a LangGraph ``messages`` channel into one string for tokenizing.

    Defensive by construction: the channel may be absent, ``None``, not a
    list, or hold heterogeneous entries (plain dicts with ``content``, the
    summarizer's ``{"__replace__": True}`` sentinel, or LangChain message
    objects). Anything we cannot read as text contributes nothing rather than
    raising — an occupancy meter must never crash the panel.
    """
    if not isinstance(messages, list):
        return ""
    parts: List[str] = []
    for m in messages:
        content: Any = None
        if isinstance(m, dict):
            content = m.get("content")
        else:
            content = getattr(m, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(str(content))
    return "\n".join(parts)


def _resolve_model_window(model_name: Optional[str]) -> Optional[int]:
    """Resolve a model's true context window from litellm's model metadata.

    Several profile build sites hardcode ``context_window`` to a flat default, so
    the meter would otherwise report the same window for every model. Looking the
    name up here keeps the reading per-model. Returns ``None`` when the name is
    empty or litellm does not know the model (e.g. a local GGUF), so the caller
    falls back to the profile's own field.
    """
    if not model_name:
        return None
    try:
        import litellm
        info = litellm.get_model_info(model_name)
    except Exception:  # noqa: BLE001 — unknown model / litellm hiccup → fall back.
        return None
    if not isinstance(info, dict):
        return None
    win = info.get("max_input_tokens") or info.get("max_tokens")
    return int(win) if isinstance(win, int) and win > 0 else None


def compute_context_occupancy(thread_id: str) -> ContextOccupancy:
    """Compute the live window occupancy for ``thread_id``.

    Reads the in-memory (L1) checkpoint for the thread, tokenizes the current
    message window with the project's precision counter, and compares it to the
    active profile's ``context_window``. Returns a zeroed result — never raises
    — when the thread is unknown, has no checkpoint yet, or carries an empty
    message window (cold start). This empty-state safety is a hard requirement:
    a brand-new thread must read 0 tokens, not throw.
    """
    window = _DEFAULT_CONTEXT_WINDOW
    used = 0
    try:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        tup = checkpoint_manager.get_tuple(config)
        if tup is not None:
            checkpoint: Dict[str, Any] = cast(Dict[str, Any], tup.checkpoint)
            values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
            if isinstance(values, dict):
                profile = values.get("active_llm_profile")
                # Profile may be a pydantic model (LLMProfile) or a plain dict
                # depending on serde round-tripping; read either shape.
                model_name = getattr(profile, "model_name", None)
                if model_name is None and isinstance(profile, dict):
                    model_name = profile.get("model_name")
                # Prefer the model's true window over a profile field that several
                # build sites pin to a flat default; fall back to the field, then
                # to the conservative module default.
                resolved = _resolve_model_window(model_name)
                if resolved:
                    window = resolved
                else:
                    cw = getattr(profile, "context_window", None)
                    if cw is None and isinstance(profile, dict):
                        cw = profile.get("context_window")
                    if isinstance(cw, int) and cw > 0:
                        window = cw
                raw_messages = values.get("messages")
                text = _serialize_messages_for_count(raw_messages)
                if text:
                    # Import lazily so this read-only route never drags tiktoken
                    # into module import for callers that don't hit it.
                    from tools.token_counter import PrecisionTokenCounter
                    used = PrecisionTokenCounter.estimate_with_buffer(text)
                else:
                    # A checkpoint exists but its message window is empty. This is
                    # normal at cold start, but if it persists across turns the
                    # window read will be stuck at zero — log enough to tell the
                    # two apart from a live trace without re-running the engine.
                    logger.debug(
                        "context occupancy: checkpoint found for thread %s but "
                        "messages channel is empty (type=%s, channels=%s)",
                        thread_id, type(raw_messages).__name__, sorted(values.keys()),
                    )
    except Exception:  # noqa: BLE001 — telemetry must degrade to zeros, never 500.
        return ContextOccupancy(
            context_window=_DEFAULT_CONTEXT_WINDOW, context_used_tokens=0, context_pct=0.0,
        )

    pct = (used / window) if window > 0 else 0.0
    return ContextOccupancy(
        context_window=window,
        context_used_tokens=used,
        context_pct=round(min(max(pct, 0.0), 1.0), 4),
    )


@router.get("/{thread_id}/context", response_model=ContextOccupancy)
async def get_session_context(thread_id: str) -> ContextOccupancy:
    """Return the live context-window occupancy for ``thread_id``.

    Feeds the context-budget meter in the workspace HUD. Read-only, per-thread,
    and empty-state safe (cold threads read 0 / default window). Mirrors the
    ``/checkpoints`` posture: loopback-trusted, surfaces only aggregate counts,
    never raises on the caller.
    """
    return compute_context_occupancy(thread_id)
