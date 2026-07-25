# ailienant-core/tools/stream_delta.py
"""Tagged streaming delta for reasoning-aware LLM streams.

The flat streaming entrypoints (``LLMGateway.astream`` / ``astream_byom``) yield
bare ``str`` text deltas. Reasoning-aware streaming bifurcates the upstream chunk
into two channels — *reasoning* tokens and *answer* tokens — so callers need a
way to discriminate them without breaking the existing ``str``-consuming
throttle/batch pipeline.

``StreamDelta`` is that discriminant: a frozen, slotted value object carrying a
``kind`` tag, the delta text, and a ``source`` provenance tag. It is intentionally
minimal (no behaviour) so it stays a pure transport concern and never leaks into
agent business logic (cognitive-isolation invariant).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StreamKind = Literal["thinking", "text"]
# Provenance of a reasoning trace: the model's own native reasoning tokens vs a
# prompt-scaffolded fallback for models without native thinking.
ReasoningSource = Literal["native", "simulated"]


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """A single discriminated delta emitted by the reasoning-aware gateway.

    - ``kind="thinking"`` — a raw reasoning-token delta (display-only; never fed
      back into the agent message history or parsed as a tool call).
    - ``kind="text"`` — an answer-token delta, identical in meaning to what the
      flat-text path yields.
    - ``source`` — provenance of the reasoning: ``"native"`` (the model's own
      reasoning channel) or ``"simulated"`` (prompt-scaffolded fallback). Only
      meaningful on ``kind="thinking"`` deltas; defaults to ``"native"`` so
      existing constructions are unaffected.
    """

    kind: StreamKind
    text: str
    source: ReasoningSource = "native"
