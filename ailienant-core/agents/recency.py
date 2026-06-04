# ailienant-core/agents/recency.py
#
# Hybrid recency signal for the Context Sufficiency Score (CSS) routing meter.
# The recency term answers "how live is the context backing this turn?" so the
# Local-vs-Cloud router can favour files that were either touched recently or
# retrieved often this session, instead of treating every file as equally stale.

import logging
from collections import OrderedDict
from datetime import datetime
from typing import Iterable

logger = logging.getLogger("RECENCY")

# Half-life of the time-decay term. After this many seconds an untouched file's
# time_decay contribution halves. One day balances "edited this morning" against
# "indexed last week" without making week-old context vanish entirely.
_HALF_LIFE_S: float = 86_400.0

# Access count that saturates the frequency term to 1.0. A handful of retrievals
# within a session is a strong "the user keeps circling this file" signal; past
# that the term is clamped so a single hot file can't dominate forever.
_FREQ_SATURATION: int = 5

# Hard bound on the heatmap so a long session over a huge repo cannot grow the
# counter without limit. Mirrors the LRU cap convention in core/blob_storage.py.
_DEFAULT_MAX_ENTRIES: int = 4096

# Relative weights of the two sub-signals; sum to 1.0 so the result stays in
# [0, 1] and feeds the ContextMeter.recency_score field (ge=0.0, le=1.0) cleanly.
_W_TIME_DECAY: float = 0.7
_W_ACCESS_FREQ: float = 0.3


def _parse_iso_to_epoch(iso: str) -> float | None:
    """Best-effort ISO-8601 → epoch seconds. Returns None on any unparseable input.

    Non-fatal by contract: a malformed timestamp in the index must never break a
    routing decision, so the bad entry is skipped rather than raised.
    """
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def compute_recency_score(
    indexed_at_iso: Iterable[str],
    buffer_mtimes: Iterable[float],
    access_count: int,
    now: float,
    half_life_s: float = _HALF_LIFE_S,
    freq_saturation: int = _FREQ_SATURATION,
) -> float:
    """Hybrid recency in [0, 1]: 0.7·time_decay + 0.3·access_frequency.

    time_decay is an exponential half-life decay over the *most recent* timestamp
    seen across the retrieved files' ``indexed_at`` and the live mtimes of the
    active/dirty IDE buffers — a hot-but-old file is rescued by its mtime, a fresh
    index beats a stale one. access_frequency is the in-session retrieval count
    normalised against ``freq_saturation`` so a frequently-circled file scores
    high even when nothing about it is chronologically fresh.

    Defensive contract (CLAUDE.md E2E rigor):
      - no parseable timestamp at all → time_decay = 0.0 (treated as maximally
        stale, never a crash and never a div-by-zero);
      - unparseable ISO entries are skipped, not raised;
      - constant denominators only (no div-by-zero);
      - result is clamped to [0, 1] so the Pydantic field validator can't reject it.
    """
    timestamps: list[float] = [
        epoch
        for iso in indexed_at_iso
        if (epoch := _parse_iso_to_epoch(iso)) is not None
    ]
    timestamps.extend(float(m) for m in buffer_mtimes)

    if timestamps:
        newest: float = max(timestamps)
        age_s: float = max(0.0, now - newest)
        # half_life_s is a positive module constant; guard anyway so a caller
        # override of 0 degrades to "no decay" instead of dividing by zero.
        time_decay: float = 0.5 ** (age_s / half_life_s) if half_life_s > 0 else 1.0
    else:
        time_decay = 0.0

    saturation: int = freq_saturation if freq_saturation > 0 else 1
    access_frequency: float = min(1.0, max(0, access_count) / saturation)

    score: float = _W_TIME_DECAY * time_decay + _W_ACCESS_FREQ * access_frequency
    return min(1.0, max(0.0, score))


class SessionAccessHeatmap:
    """Bounded in-session per-file retrieval counter (LRU eviction on overflow).

    Process-singleton (see ``session_heatmap`` below) so counts persist across
    planner turns within a session, giving the frequency term memory of which
    files the user keeps pulling into context. Keyed by ``project_id::path`` to
    keep tenants isolated. Single-event-loop, single-process — no locking needed.
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._counts: "OrderedDict[str, int]" = OrderedDict()
        self._max_entries = max_entries

    @staticmethod
    def _key(project_id: str, path: str) -> str:
        return f"{project_id}::{path}"

    def bump(self, project_id: str, paths: Iterable[str]) -> None:
        """Increment the retrieval count for each path, evicting LRU on overflow."""
        for path in paths:
            if not path:
                continue
            key = self._key(project_id, path)
            self._counts[key] = self._counts.get(key, 0) + 1
            self._counts.move_to_end(key)
            if len(self._counts) > self._max_entries:
                evicted, _ = self._counts.popitem(last=False)
                logger.warning(
                    "SessionAccessHeatmap eviction: %s dropped (at capacity=%d). "
                    "If counts are being lost prematurely, raise max_entries.",
                    evicted, self._max_entries,
                )

    def count(self, project_id: str, path: str) -> int:
        """Current in-session retrieval count for one file (0 if never seen)."""
        return self._counts.get(self._key(project_id, path), 0)

    def reset(self) -> None:
        """Clear all counts. Used for test isolation (mirrors token_ledger.reset)."""
        self._counts.clear()


# Process-singleton, same shape as token_ledger / blob_storage.
session_heatmap: SessionAccessHeatmap = SessionAccessHeatmap()
