# ailienant-core/tools/stream_delta.py
"""Phase 9 (ADR-707) — tagged streaming delta for Native Thinking.

The legacy streaming entrypoints (``LLMGateway.astream`` / ``astream_byom``)
yield bare ``str`` text deltas. Native Thinking bifurcates the upstream chunk
into two channels — *reasoning* tokens and *answer* tokens — so callers need a
way to discriminate them without breaking the existing ``str``-consuming
throttle/batch pipeline.

``StreamDelta`` is that discriminant: a frozen, slotted value object carrying a
``kind`` tag plus the delta text. It is intentionally minimal (no behaviour) so
it stays a pure transport concern and never leaks into agent business logic
(cognitive-isolation invariant, Phase 9 plan §Hard constraints).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StreamKind = Literal["thinking", "text"]


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """A single discriminated delta emitted by the thinking-aware gateway.

    - ``kind="thinking"`` — a raw reasoning-token delta (display-only; never fed
      back into the agent message history or parsed as a tool call).
    - ``kind="text"`` — an answer-token delta, identical in meaning to what the
      legacy flat-text path yields.
    """

    kind: StreamKind
    text: str
