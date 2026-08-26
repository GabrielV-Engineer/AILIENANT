# ailienant-core/tests/test_effort_budget.py
"""The Effort Budget (13.1.3, Track D) — light/balanced/deep verification depth.

Replaces the SEQUENTIAL/MICRO_SWARM/FULL_SWARM execution-mode selector, which
never controlled anything the main graph actually ran (OQ-4/N7): the topology
channel was written, persisted, and read back on resume, but no routing
decision in brain/engine.py ever branched on it. Effort genuinely controls
three things (brain/retry_policy.py): the self-heal attempt ceiling, whether
the lint/LSP gate runs, and whether the plan's own acceptance checks execute.
"""
from __future__ import annotations

import pytest

from brain.retry_policy import (
    CORRECTION_MAX_ATTEMPTS,
    DEFAULT_EFFORT_LEVEL,
    normalize_effort_level,
    resolve_correction_ceiling,
)


# ── resolve_correction_ceiling ──────────────────────────────────────────────


def test_light_effort_gets_zero_self_heal_attempts() -> None:
    """The fast, cheap path exists precisely so a trivial turn does not pay
    CORRECTION_MAX_ATTEMPTS's full cost on hardware where each retry is
    expensive — a syntax/exec failure fails the step outright."""
    assert resolve_correction_ceiling("light") == 0


def test_balanced_and_deep_keep_the_existing_ceiling() -> None:
    assert resolve_correction_ceiling("balanced") == CORRECTION_MAX_ATTEMPTS
    assert resolve_correction_ceiling("deep") == CORRECTION_MAX_ATTEMPTS


def test_unrecognized_or_absent_level_falls_back_to_the_flat_ceiling() -> None:
    """An older checkpoint or a caller that never set effort_level must see
    today's exact unbounded-by-effort behaviour, not a silent zero."""
    assert resolve_correction_ceiling(None) == CORRECTION_MAX_ATTEMPTS
    assert resolve_correction_ceiling("") == CORRECTION_MAX_ATTEMPTS
    assert resolve_correction_ceiling("nonsense") == CORRECTION_MAX_ATTEMPTS


# ── normalize_effort_level ──────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["light", "balanced", "deep"])
def test_normalize_accepts_known_levels_case_insensitively(value: str) -> None:
    assert normalize_effort_level(value) == value
    assert normalize_effort_level(value.upper()) == value


def test_normalize_falls_back_to_the_default_for_anything_else() -> None:
    assert normalize_effort_level(None) == DEFAULT_EFFORT_LEVEL
    assert normalize_effort_level("") == DEFAULT_EFFORT_LEVEL
    assert normalize_effort_level("SEQUENTIAL") == DEFAULT_EFFORT_LEVEL
    assert normalize_effort_level("thorough") == DEFAULT_EFFORT_LEVEL  # an old candidate name, not a real level


def test_default_effort_level_is_balanced() -> None:
    assert DEFAULT_EFFORT_LEVEL == "balanced"


# ── core.execution_mode — the process-global preference store ──────────────


def test_execution_mode_get_set_roundtrip() -> None:
    from core import execution_mode

    original = execution_mode.get_effort_level()
    try:
        execution_mode.set_effort_level("deep")
        assert execution_mode.get_effort_level() == "deep"
        execution_mode.set_effort_level("light")
        assert execution_mode.get_effort_level() == "light"
    finally:
        execution_mode.set_effort_level(original)


def test_execution_mode_set_normalizes_an_unrecognized_value() -> None:
    from core import execution_mode

    original = execution_mode.get_effort_level()
    try:
        execution_mode.set_effort_level("FULL_SWARM")  # the old, now-meaningless value
        assert execution_mode.get_effort_level() == DEFAULT_EFFORT_LEVEL
    finally:
        execution_mode.set_effort_level(original)


# ── tools.llm_gateway.estimate_effort_costs ─────────────────────────────────


def test_estimate_effort_costs_covers_all_three_levels() -> None:
    from tools.llm_gateway import estimate_effort_costs

    estimates = estimate_effort_costs(None)
    assert set(estimates.keys()) == {"light", "balanced", "deep"}
    for level, info in estimates.items():
        assert "extra_calls" in info
        assert "seconds_per_extra_call" in info
        assert info["seconds_per_extra_call"] > 0


def test_estimate_effort_costs_reports_uncalibrated_without_history() -> None:
    from tools.llm_gateway import estimate_effort_costs

    estimates = estimate_effort_costs("a-model-with-no-completion-history")
    assert all(info["calibrated"] is False for info in estimates.values())


def test_estimate_effort_costs_reports_calibrated_once_the_model_has_history() -> None:
    from tools.llm_gateway import _local_model_completions, _record_local_completion, estimate_effort_costs

    model = "test-calibrated-model"
    _local_model_completions.pop(model, None)
    try:
        _record_local_completion(model, 500, 10.0)
        _record_local_completion(model, 500, 10.0)
        estimates = estimate_effort_costs(model)
        assert all(info["calibrated"] is True for info in estimates.values())
    finally:
        _local_model_completions.pop(model, None)


def test_light_never_costs_more_than_balanced_or_deep() -> None:
    """The estimate's own ordering must match the real policy: light does zero
    extra local calls, so it must never claim a cost the others don't also incur."""
    from tools.llm_gateway import estimate_effort_costs

    estimates = estimate_effort_costs(None)
    assert estimates["light"]["extra_calls"] == "0"
