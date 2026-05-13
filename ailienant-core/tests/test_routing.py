# ailienant-core/tests/test_routing.py
#
# DoD: pytest tests/test_routing.py -v must pass with 0 failures.

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
