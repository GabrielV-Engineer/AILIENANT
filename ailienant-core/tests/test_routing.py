# ailienant-core/tests/test_routing.py
#
# DoD: pytest tests/test_routing.py -v must pass with 0 failures.

import math

from brain.routing_engine import RoutingEngine

# ---------------------------------------------------------------------------
# derive_routing_decision — empty-corpus discrimination
# ---------------------------------------------------------------------------
#
# get_optimal_provider/resolve_provider (RoutingEngine's own duplicate CSS/TCI
# matrices) and their tests were removed with them (13.1.3, N10) — neither had
# a production caller. derive_routing_decision below is the single live
# implementation every agent actually consumes.


def test_empty_corpus_skips_red_alert_floor() -> None:
    """An empty corpus + simple task routes LOCAL_SMALL despite a low CSS.

    A cold/tiny workspace has nothing to retrieve, so a low CSS is a cold-start
    artifact — not a coverage gap — and must not escalate to CLOUD.
    """
    from core.memory.context_auditor import derive_routing_decision

    assert derive_routing_decision(tci=20.0, css=10.0, corpus_empty=True) == "LOCAL_SMALL"


def test_non_empty_corpus_low_css_still_clouds() -> None:
    """Regression guard: a real corpus with low coverage keeps the CLOUD red-alert floor."""
    from core.memory.context_auditor import derive_routing_decision

    assert derive_routing_decision(tci=20.0, css=10.0, corpus_empty=False) == "CLOUD"
    # Default (param omitted) must preserve the original red-alert behavior.
    assert derive_routing_decision(tci=20.0, css=10.0) == "CLOUD"


def test_empty_corpus_does_not_override_high_tci_band() -> None:
    """Skipping the CSS floor only defers to the TCI bands — TCI≥75 still routes CLOUD."""
    from core.memory.context_auditor import derive_routing_decision

    assert derive_routing_decision(tci=80.0, css=10.0, corpus_empty=True) == "CLOUD"


# ---------------------------------------------------------------------------
# resolve_model_alias_for_routing — the routing decision actually selecting
# a model tier (13.1.3, N9). Full 4-way coverage including the LOCAL_MEDIUM
# band carved out of the old single LOCAL_BIG range.
# ---------------------------------------------------------------------------


def test_resolve_model_alias_maps_each_routing_decision() -> None:
    from core.memory.context_auditor import resolve_model_alias_for_routing
    from shared.config import MODEL_BIG, MODEL_CLOUD, MODEL_MEDIUM, MODEL_SMALL

    assert resolve_model_alias_for_routing("LOCAL_SMALL", default=MODEL_BIG) == MODEL_SMALL
    assert resolve_model_alias_for_routing("LOCAL_MEDIUM", default=MODEL_BIG) == MODEL_MEDIUM
    assert resolve_model_alias_for_routing("LOCAL_BIG", default=MODEL_SMALL) == MODEL_BIG
    # CLOUD maps to MODEL_BIG, not a dedicated cloud alias — matches the
    # existing core/resource_manager.py SWITCH_TO_CLOUD precedent.
    # CLOUD reaches the preset's own cloud tier; collapsing it onto MODEL_BIG
    # made the top of the escalation ladder unreachable from every agent path.
    assert resolve_model_alias_for_routing("CLOUD", default=MODEL_SMALL) == MODEL_CLOUD


def test_resolve_model_alias_falls_back_when_decision_is_absent() -> None:
    """A cache-hit turn or a benchmark stub has no computed decision yet —
    the caller's own default must win, unchanged from today's behaviour."""
    from core.memory.context_auditor import resolve_model_alias_for_routing
    from shared.config import MODEL_MEDIUM

    assert resolve_model_alias_for_routing(None, default=MODEL_MEDIUM) == MODEL_MEDIUM


def test_resolve_model_alias_falls_back_on_an_unrecognized_value() -> None:
    from core.memory.context_auditor import resolve_model_alias_for_routing
    from shared.config import MODEL_BIG

    assert resolve_model_alias_for_routing("HUMAN_REQUIRED", default=MODEL_BIG) == MODEL_BIG


# ---------------------------------------------------------------------------
# HardwareDetector — basic sanity (no GPU required)
# ---------------------------------------------------------------------------

def test_hardware_profile_has_required_fields() -> None:
    from shared.hardware import HardwareDetector, HardwareProfile

    profile = HardwareDetector.detect()
    assert isinstance(profile, HardwareProfile)
    assert profile.os_type in ("windows", "macos", "linux")
    assert profile.vram_gb >= 0.0
    assert profile.ram_gb >= 0.0
    assert isinstance(profile.is_apple_silicon, bool)


# ---------------------------------------------------------------------------
# PrecisionTokenCounter — safety buffer correctness
# ---------------------------------------------------------------------------

def test_token_counter_safety_buffer() -> None:
    from tools.token_counter import PrecisionTokenCounter

    text = "hello world"
    raw = PrecisionTokenCounter.count(text)
    buffered = PrecisionTokenCounter.estimate_with_buffer(text)
    assert buffered >= raw
    assert buffered == math.ceil(raw * 1.10)


def test_token_counter_buffer_on_empty_string() -> None:
    from tools.token_counter import PrecisionTokenCounter

    raw = PrecisionTokenCounter.count("")
    buffered = PrecisionTokenCounter.estimate_with_buffer("")
    # math.ceil(0 * 1.10) == 0; must not raise
    assert buffered == math.ceil(raw * 1.10)


def test_token_counter_unknown_model_falls_back() -> None:
    from tools.token_counter import PrecisionTokenCounter

    # Should not raise; falls back to cl100k_base
    result = PrecisionTokenCounter.count("test", model="nonexistent-model-xyz")
    assert result > 0


# ---------------------------------------------------------------------------
# RoutingEngine.get_keep_alive — tiered VRAM residency
# ---------------------------------------------------------------------------

def test_keep_alive_big_model_is_5m() -> None:
    from shared.config import MODEL_BIG

    result = RoutingEngine.get_keep_alive(MODEL_BIG)
    assert result == "5m"


def test_keep_alive_small_model_is_permanent() -> None:
    from shared.config import MODEL_SMALL

    result = RoutingEngine.get_keep_alive(MODEL_SMALL)
    assert result == -1


def test_keep_alive_unknown_model_is_permanent() -> None:
    # Unknown alias → conservative permanent residency (no VRAM release assumed)
    result = RoutingEngine.get_keep_alive("some-unknown-alias")
    assert result == -1
