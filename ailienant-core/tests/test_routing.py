# ailienant-core/tests/test_routing.py
#
# DoD: pytest tests/test_routing.py -v must pass with 0 failures.

import math
import pytest

from brain.routing_engine import RoutingEngine


# ---------------------------------------------------------------------------
# get_optimal_provider — 2D routing matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tci,css,expected", [
    (20.0,  75.0, "LOCAL"),           # Low TCI, healthy CSS → privacy-first local
    (85.0,  75.0, "CLOUD"),           # High TCI, healthy CSS → cloud cognitive power
    (50.0,  30.0, "HUMAN_REQUIRED"),  # Low CSS → graceful degradation gate
    (29.9,  60.0, "LOCAL"),           # Boundary: just below 30 → local
    (30.0,  60.0, "CLOUD"),           # Boundary: exactly 30 → cloud
    (90.0,  39.9, "HUMAN_REQUIRED"),  # CSS < 40 takes priority even over High-TCI
    (0.0,    0.0, "HUMAN_REQUIRED"),  # Zero everything → human required
    (100.0, 100.0, "CLOUD"),          # Max TCI, max CSS → cloud
])
def test_get_optimal_provider(tci: float, css: float, expected: str) -> None:
    assert RoutingEngine.get_optimal_provider(tci, css) == expected


def test_local_threshold_is_exclusive() -> None:
    """TCI=30 must route CLOUD, not LOCAL — the boundary belongs to CLOUD."""
    assert RoutingEngine.get_optimal_provider(tci=30.0, css=50.0) == "CLOUD"


def test_css_gate_overrides_high_tci() -> None:
    """Even TCI=100 must not reach CLOUD when CSS is below the red-alert threshold."""
    assert RoutingEngine.get_optimal_provider(tci=100.0, css=39.99) == "HUMAN_REQUIRED"


def test_css_boundary_at_40() -> None:
    """CSS=40.0 is the minimum acceptable; anything below triggers HUMAN_REQUIRED."""
    assert RoutingEngine.get_optimal_provider(tci=50.0, css=40.0) != "HUMAN_REQUIRED"
    assert RoutingEngine.get_optimal_provider(tci=50.0, css=39.99) == "HUMAN_REQUIRED"


# ---------------------------------------------------------------------------
# derive_routing_decision — empty-corpus discrimination
# ---------------------------------------------------------------------------

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
# resolve_provider — Vision Bypass
# ---------------------------------------------------------------------------

def test_vision_bypass_routes_cloud() -> None:
    """Images present + cloud available → CLOUD regardless of low TCI."""
    provider, warning = RoutingEngine.resolve_provider(
        tci=10.0, css=80.0, has_images=True, cloud_available=True
    )
    assert provider == "CLOUD"
    assert warning is None


def test_vision_bypass_no_cloud_requires_human() -> None:
    """Images present but cloud unavailable → HUMAN_REQUIRED (cannot process locally)."""
    provider, warning = RoutingEngine.resolve_provider(
        tci=10.0, css=80.0, has_images=True, cloud_available=False
    )
    assert provider == "HUMAN_REQUIRED"


# ---------------------------------------------------------------------------
# resolve_provider — Cloud Guard / Graceful Degradation
# ---------------------------------------------------------------------------

def test_no_cloud_high_tci_falls_back_to_local_with_warning() -> None:
    """High-TCI, cloud unavailable, no images → LOCAL with a non-empty routing_warning."""
    provider, warning = RoutingEngine.resolve_provider(
        tci=80.0, css=70.0, has_images=False, cloud_available=False
    )
    assert provider == "LOCAL"
    assert warning is not None and len(warning) > 0


def test_no_cloud_low_tci_returns_local_no_warning() -> None:
    """Low-TCI never needed CLOUD — no warning when cloud is unavailable."""
    provider, warning = RoutingEngine.resolve_provider(
        tci=20.0, css=70.0, has_images=False, cloud_available=False
    )
    assert provider == "LOCAL"
    assert warning is None


def test_css_gate_overrides_cloud_fallback() -> None:
    """CSS < 40 must return HUMAN_REQUIRED even if cloud is unavailable (no leaking to LOCAL)."""
    provider, _ = RoutingEngine.resolve_provider(
        tci=50.0, css=35.0, cloud_available=False
    )
    assert provider == "HUMAN_REQUIRED"


def test_resolve_provider_cloud_available_high_tci() -> None:
    """Sanity: high-TCI + healthy CSS + cloud → CLOUD, no warning."""
    provider, warning = RoutingEngine.resolve_provider(
        tci=85.0, css=75.0, cloud_available=True
    )
    assert provider == "CLOUD"
    assert warning is None


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
