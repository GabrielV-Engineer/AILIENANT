# ailienant-core/transport/token_batcher.py
#
# Phase 7.10.2 — Cognitive Transparency & Token Batching (ADR-702).
#
# Two cooperating units that protect the Extension-Host <-> Webview IPC bridge
# from frame-rate collapse (G1) while keeping the user informed:
#
#   1. batch_tokens()  — an async-generator coalescer. Merges every outbound text
#      delta that arrives inside a `chunk_ms` (default 40 ms) window into ONE WS
#      frame, instead of one frame per token. Sibling of transport.throttler;
#      composes as  throttled_stream(batch_tokens(gen), ws)  — batch first, then
#      backpressure-guard (blueprint §4.2). Modeled on the timer-flush shape of
#      core.io_coalescer, but it CONCATENATES ordered deltas (no key dedupe).
#
#   2. NarrationGate   — a byte-accounting budget that keeps `server_pipeline_step`
#      narration <= 15 % of streamed volume *once the answer channel is live*
#      (ADR-702 TR2). Before any answer bytes exist (the pre-answer phases:
#      context_gather -> routing_decision -> drafting_spec) narration is allowed
#      unconditionally, so structural telemetry never freezes the screen.
#
# Flush timing uses the monotonic event-loop clock (loop.time()) and is decided
# INLINE as each delta arrives — no background tasks, no redundant timers — so a
# stalled upstream never blocks the event loop.

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List

# Default coalescing window. 40 ms ~= 25 frames/s of WS writes, comfortably below
# the Webview's >= 45 FPS render budget while still feeling real-time.
DEFAULT_CHUNK_MS: int = 40

# Token-count safety valve: a single high-volume burst (e.g. 7.11's inline
# diff-stream canvas) flushes on size before the time window, bounding memory.
DEFAULT_MAX_BUFFER_CHARS: int = 4096

# ADR-702 TR2 — narration must not exceed this fraction of streamed volume once
# the answer is actively streaming.
MAX_NARRATION_RATIO: float = 0.15


async def batch_tokens(
    source: AsyncIterator[str],
    chunk_ms: int = DEFAULT_CHUNK_MS,
    max_buffer_chars: int = DEFAULT_MAX_BUFFER_CHARS,
) -> AsyncIterator[str]:
    """Coalesce an async token stream into ~`chunk_ms`-spaced merged frames.

    Yields the concatenation of all deltas accumulated within each window, in
    order, with no loss: ``"".join(batch_tokens(src)) == "".join(src)``. A frame
    is emitted when EITHER the elapsed window reaches ``chunk_ms`` OR the buffer
    reaches ``max_buffer_chars`` (whichever trips first); the trailing partial
    buffer is always flushed when ``source`` is exhausted.

    Empty deltas are dropped (they carry no characters and would only produce
    empty WS frames); this does not affect the concatenation identity.
    """
    loop = asyncio.get_running_loop()
    buffer: List[str] = []
    buffered_chars: int = 0
    window_start: float = loop.time()
    threshold_s: float = chunk_ms / 1000.0

    async for delta in source:
        if not delta:
            continue
        buffer.append(delta)
        buffered_chars += len(delta)

        elapsed = loop.time() - window_start
        if elapsed >= threshold_s or buffered_chars >= max_buffer_chars:
            yield "".join(buffer)
            buffer.clear()
            buffered_chars = 0
            window_start = loop.time()

    if buffer:
        yield "".join(buffer)


class NarrationGate:
    """Bandwidth budget for `server_pipeline_step` narration (ADR-702 TR2).

    Tracks cumulative answer bytes vs. narration bytes and decides whether the
    next narration packet may be emitted.

    Cold-start rule (deadlock fix): while the answer channel is silent
    (``answer_bytes == 0``) — the pre-answer phases where the model has produced
    no text yet — ``allow()`` always returns True and the packet is NOT charged
    to the budget. Pre-answer transparency is "free"; suppressing it would freeze
    the UI and defeat the feature. Enforcement begins only after the first
    ``record_answer()`` with a positive count.
    """

    def __init__(self, max_ratio: float = MAX_NARRATION_RATIO) -> None:
        self._max_ratio: float = max_ratio
        self._answer_bytes: int = 0
        self._narration_bytes: int = 0

    @property
    def answer_bytes(self) -> int:
        return self._answer_bytes

    @property
    def narration_bytes(self) -> int:
        """Narration bytes counted against the budget (post-streaming only)."""
        return self._narration_bytes

    def record_answer(self, n_bytes: int) -> None:
        """Add to the cumulative answer volume. Flips the gate into enforcement."""
        if n_bytes > 0:
            self._answer_bytes += n_bytes

    def allow(self, narration_bytes: int) -> bool:
        """Return True iff this narration packet may be emitted now.

        Pre-answer (cold start): always True, uncharged. Once streaming has
        begun, True only while the projected ratio
        ``narration / (answer + narration)`` stays at or below ``max_ratio``;
        accepted packets are charged to the budget, rejected ones are not.
        """
        # Cold start — answer channel silent: never suppress, never charge.
        if self._answer_bytes == 0:
            return True

        projected_narration = self._narration_bytes + narration_bytes
        total = self._answer_bytes + projected_narration
        if total == 0:
            return True
        if projected_narration / total <= self._max_ratio:
            self._narration_bytes = projected_narration
            return True
        return False
