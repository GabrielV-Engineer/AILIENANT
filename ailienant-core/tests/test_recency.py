# tests/test_recency.py
"""DoD matrix for the Session-Heatmap Recency signal (agents/recency.py).

Covers the hybrid recency contract that replaced the static placeholder in the
planner's CSS routing meter:
    - hot-but-old beats cold-but-old (access-frequency term fires);
    - fresh beats stale (time-decay term fires);
    - empty / unparseable / zero inputs degrade safely (no raise, no div-by-zero);
    - the session heatmap is hard-bounded (LRU eviction).
"""
from __future__ import annotations

import logging

import pytest

from agents.recency import (
    _FREQ_SATURATION,
    _HALF_LIFE_S,
    SessionAccessHeatmap,
    compute_recency_score,
    session_heatmap,
)

# A fixed clock so every decay assertion is deterministic.
_NOW: float = 1_000_000_000.0


@pytest.fixture(autouse=True)
def _reset_singleton() -> object:
    session_heatmap.reset()
    yield
    session_heatmap.reset()


# ── compute_recency_score ────────────────────────────────────────────────────


def test_hot_but_old_beats_cold_but_old() -> None:
    """Same stale index, but a frequently-accessed file must outrank an untouched one."""
    old_iso: str = "2000-01-01T00:00:00+00:00"  # ancient → time_decay ≈ 0 for both

    cold = compute_recency_score(
        indexed_at_iso=[old_iso], buffer_mtimes=[], access_count=0, now=_NOW
    )
    hot = compute_recency_score(
        indexed_at_iso=[old_iso], buffer_mtimes=[], access_count=_FREQ_SATURATION, now=_NOW
    )
    assert hot > cold
    # The only live signal is frequency: 0.3 * 1.0.
    assert hot == pytest.approx(0.3)
    assert cold == pytest.approx(0.0)


def test_fresh_beats_stale_at_equal_access() -> None:
    """A just-indexed file must outrank an old one when access frequency is equal."""
    fresh_epoch: float = _NOW - 1.0          # ~now → decay ≈ 1.0
    stale_iso: str = "2000-01-01T00:00:00+00:00"

    fresh = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[fresh_epoch], access_count=1, now=_NOW
    )
    stale = compute_recency_score(
        indexed_at_iso=[stale_iso], buffer_mtimes=[], access_count=1, now=_NOW
    )
    assert fresh > stale


def test_buffer_mtime_rescues_hot_but_unindexed_file() -> None:
    """A freshly-edited buffer (recent mtime) lifts time_decay even with no index."""
    score = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[_NOW], access_count=0, now=_NOW
    )
    # 0.7 * decay(age≈0) ≈ 0.7, frequency term zero.
    assert score == pytest.approx(0.7, abs=1e-6)


def test_half_life_halves_decay() -> None:
    """At exactly one half-life of age the time-decay contribution is halved."""
    aged_epoch: float = _NOW - _HALF_LIFE_S
    score = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[aged_epoch], access_count=0, now=_NOW
    )
    # decay = 0.5 → 0.7 * 0.5 = 0.35.
    assert score == pytest.approx(0.35, abs=1e-6)


def test_newest_timestamp_wins_across_sources() -> None:
    """time_decay keys off the most recent of index + buffer timestamps."""
    stale_iso: str = "2000-01-01T00:00:00+00:00"
    score = compute_recency_score(
        indexed_at_iso=[stale_iso], buffer_mtimes=[_NOW], access_count=0, now=_NOW
    )
    # The fresh buffer mtime dominates → decay ≈ 1.0 → ≈ 0.7.
    assert score == pytest.approx(0.7, abs=1e-6)


def test_empty_inputs_safe_default() -> None:
    """No timestamps and zero access → 0.0, no exception, no div-by-zero."""
    assert compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[], access_count=0, now=_NOW
    ) == 0.0


def test_unparseable_iso_is_skipped_not_raised() -> None:
    """A malformed ISO entry is ignored; the call must not raise."""
    score = compute_recency_score(
        indexed_at_iso=["not-a-date", ""], buffer_mtimes=[], access_count=0, now=_NOW
    )
    assert score == 0.0  # both entries skipped → time_decay 0, freq 0


def test_mixed_valid_and_invalid_iso() -> None:
    """A valid fresh ISO survives alongside garbage entries."""
    fresh_iso: str = "2030-01-01T00:00:00+00:00"  # future → age clamped to 0 → decay 1.0
    score = compute_recency_score(
        indexed_at_iso=["garbage", fresh_iso], buffer_mtimes=[], access_count=0, now=_NOW
    )
    assert score == pytest.approx(0.7, abs=1e-6)


def test_access_count_saturates() -> None:
    """Frequency term clamps at 1.0 past the saturation threshold."""
    below = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[], access_count=_FREQ_SATURATION, now=_NOW
    )
    above = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[], access_count=_FREQ_SATURATION * 10, now=_NOW
    )
    assert below == above == pytest.approx(0.3)


def test_result_clamped_to_unit_interval() -> None:
    """Even a fresh + maximally-accessed file stays within [0, 1]."""
    score = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[_NOW], access_count=_FREQ_SATURATION * 5, now=_NOW
    )
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(1.0, abs=1e-6)  # 0.7 + 0.3


def test_negative_access_count_floored() -> None:
    """A nonsensical negative count is floored to zero, never negative."""
    score = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[], access_count=-5, now=_NOW
    )
    assert score == 0.0


def test_zero_half_life_override_degrades_to_no_decay() -> None:
    """A 0 half_life override must not divide by zero; treat as no decay (1.0)."""
    score = compute_recency_score(
        indexed_at_iso=[], buffer_mtimes=[_NOW - 999_999.0],
        access_count=0, now=_NOW, half_life_s=0.0,
    )
    assert score == pytest.approx(0.7, abs=1e-6)


# ── SessionAccessHeatmap ─────────────────────────────────────────────────────


def test_heatmap_counts_and_tenant_isolation() -> None:
    hm = SessionAccessHeatmap()
    hm.bump("proj-a", ["x.py", "x.py", "y.py"])
    assert hm.count("proj-a", "x.py") == 2
    assert hm.count("proj-a", "y.py") == 1
    # Same path under a different tenant is a distinct key.
    assert hm.count("proj-b", "x.py") == 0


def test_heatmap_ignores_empty_paths() -> None:
    hm = SessionAccessHeatmap()
    hm.bump("proj", ["", "real.py", ""])
    assert hm.count("proj", "real.py") == 1


def test_heatmap_reset_clears_counts() -> None:
    hm = SessionAccessHeatmap()
    hm.bump("proj", ["a.py"])
    hm.reset()
    assert hm.count("proj", "a.py") == 0


def test_heatmap_is_bounded_with_lru_eviction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past max_entries the oldest key is evicted and a WARNING is logged."""
    hm = SessionAccessHeatmap(max_entries=3)
    with caplog.at_level(logging.WARNING):
        hm.bump("p", ["a.py", "b.py", "c.py"])
        assert hm.count("p", "a.py") == 1  # still present at capacity
        hm.bump("p", ["d.py"])             # overflow → evict LRU ("a.py")
    assert hm.count("p", "a.py") == 0      # evicted
    assert hm.count("p", "d.py") == 1
    # Bound holds: never more than max_entries keys retained.
    assert len(hm._counts) == 3
    assert any("eviction" in rec.message for rec in caplog.records)
