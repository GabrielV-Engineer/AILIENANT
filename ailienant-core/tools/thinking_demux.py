"""Incremental splitter for a scaffolded ``<thinking>…</thinking>`` stream.

Models without native reasoning are prompted (via a system-prompt scaffold) to
narrate their reasoning inside a single ``<thinking>…</thinking>`` block and then
emit the final answer. This demuxer consumes the resulting **flat** token stream
and separates it, on the fly, into two channels:

- the reasoning prose inside the block (streamed live to the reasoning UI), and
- the answer that follows the closing tag.

It is robust to tags split across chunk boundaries (a carry buffer holds any
suffix that could be the start of a tag) and never emits the tag text itself.

Contract (answer mission-critical, reasoning best-effort):

- Text **before** ``<thinking>`` is held; on open it is **discarded** (a model
  preamble), so it never leaks into the answer.
- If ``<thinking>`` never opens, the whole stream is treated as the answer at
  :meth:`finish` (the model ignored the scaffold — degrade gracefully).
- If it opens but never closes, the answer is **empty** (no leak of reasoning
  into the answer); the caller's retry path handles the empty result.

Tag matching is exact and case-sensitive — the scaffold emits the exact tags.
"""

from __future__ import annotations

from enum import Enum, auto

_OPEN_TAG = "<thinking>"
_CLOSE_TAG = "</thinking>"


class _State(Enum):
    SEEKING_OPEN = auto()
    INSIDE = auto()
    AFTER_CLOSE = auto()


def _suffix_prefix_len(s: str, tag: str) -> int:
    """Largest ``k`` (``0 < k < len(tag)``) with ``s`` ending in ``tag[:k]``.

    Used to hold back a partial tag straddling a chunk boundary. A full match is
    handled by ``str.find`` before this is ever consulted, so ``k`` stays below
    ``len(tag)``.
    """
    max_k = min(len(s), len(tag) - 1)
    for k in range(max_k, 0, -1):
        if s[-k:] == tag[:k]:
            return k
    return 0


class ThinkingTagDemuxer:
    """Stateful, chunk-boundary-safe splitter for one scaffolded stream.

    One instance per stream. Feed flat text deltas; each call returns the
    ``(reasoning_delta, answer_delta)`` resolvable so far. Call :meth:`finish`
    once the stream ends to flush any tail per the degrade rules above.
    """

    __slots__ = ("_state", "_carry")

    def __init__(self) -> None:
        self._state = _State.SEEKING_OPEN
        # Unresolved text: in SEEKING_OPEN it is the buffered preamble (may turn
        # out to be the answer); in INSIDE it is a possible partial close tag.
        self._carry = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        """Consume a flat delta; return ``(reasoning_delta, answer_delta)``."""
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        work = self._carry + chunk
        self._carry = ""

        while work:
            if self._state is _State.SEEKING_OPEN:
                idx = work.find(_OPEN_TAG)
                if idx == -1:
                    # No open tag yet — hold everything. It is either preamble
                    # (discarded once a tag opens) or the whole answer (flushed
                    # at finish if a tag never opens).
                    self._carry = work
                    break
                # Opened: discard the preamble (work[:idx]) and the tag; continue
                # processing the remainder in the INSIDE state.
                work = work[idx + len(_OPEN_TAG):]
                self._state = _State.INSIDE

            elif self._state is _State.INSIDE:
                idx = work.find(_CLOSE_TAG)
                if idx == -1:
                    keep = _suffix_prefix_len(work, _CLOSE_TAG)
                    emit = work[: len(work) - keep]
                    self._carry = work[len(work) - keep:]
                    if emit:
                        thinking_parts.append(emit)
                    break
                if idx > 0:
                    thinking_parts.append(work[:idx])
                work = work[idx + len(_CLOSE_TAG):]
                self._state = _State.AFTER_CLOSE

            else:  # AFTER_CLOSE — everything left is answer.
                answer_parts.append(work)
                work = ""

        return "".join(thinking_parts), "".join(answer_parts)

    def finish(self) -> tuple[str, str]:
        """Flush the tail at end-of-stream; return ``(reasoning, answer)``."""
        carry = self._carry
        self._carry = ""
        if self._state is _State.SEEKING_OPEN:
            # A tag never opened — the model ignored the scaffold; the buffered
            # text is the answer.
            return "", carry
        if self._state is _State.INSIDE:
            # Opened but never closed — emit the trailing reasoning, but keep the
            # answer empty so no reasoning ever leaks into it.
            return carry, ""
        # AFTER_CLOSE — any residue is answer (normally empty).
        return "", carry
