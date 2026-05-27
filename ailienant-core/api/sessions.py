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

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from brain.checkpoint import checkpoint_manager

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
