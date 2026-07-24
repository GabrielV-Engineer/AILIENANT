"""BYOM per-model cost resolution — dashboard cost badges.

Certifies the honesty rules of ``core.config.model_pricing``: local providers are
free without touching litellm, cloud models resolve through the canonical-id
candidate ladder against litellm's cost map (per-token → per-1M-token), and an
unknown model yields ``None`` (the UI shows no badge, never a guessed figure).
Lookups are sealed against litellm's bundled data with a controlled map so the
test can't drift when a provider changes rates; a final smoke asserts the real
wiring end-to-end.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from core.config import model_pricing
from core.config.model_pricing import ModelPrice, price_for
from core.config.byom_config import ModelPreset
from api.byom import DiscoveredModelItem, _build_model_pricing

# A controlled cost map exercising both key shapes we normalize onto: litellm's
# provider-prefixed gemini key and its bare openai key.
_FAKE_COST_MAP: Dict[str, Any] = {
    "gemini/gemini-2.0-flash": {"input_cost_per_token": 1.0e-7, "output_cost_per_token": 4.0e-7},
    "gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 1.0e-5},
}


@pytest.fixture()
def fake_cost_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_pricing, "_litellm_cost_map", lambda: dict(_FAKE_COST_MAP))


# ── Local providers are free, without consulting litellm ──────────────────────


def test_local_model_is_free_without_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the local path ever touched the map, this exploding stub would surface it.
    def _boom() -> Dict[str, Any]:
        raise AssertionError("local pricing must not consult litellm")

    monkeypatch.setattr(model_pricing, "_litellm_cost_map", _boom)
    price = price_for("ollama/phi3")
    assert price == ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0, local=True)


def test_custom_local_provider_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_pricing, "_litellm_cost_map", dict)  # empty map
    price = price_for("custom/my-local-model")
    assert price is not None and price.local is True


# ── Cloud models resolve through the candidate ladder ─────────────────────────


def test_cloud_model_via_normalized_prefix(fake_cost_map: None) -> None:
    # "google/…" normalizes to litellm's "gemini/…" key (first candidate).
    price = price_for("google/gemini-2.0-flash")
    assert price is not None and not price.local
    assert price.input_per_mtok == pytest.approx(0.1)   # 1.0e-7 * 1e6
    assert price.output_per_mtok == pytest.approx(0.4)  # 4.0e-7 * 1e6


def test_cloud_model_via_bare_stem(fake_cost_map: None) -> None:
    # "openai/gpt-4o" isn't a litellm key; the bare "gpt-4o" stem candidate is.
    price = price_for("openai/gpt-4o")
    assert price is not None and not price.local
    assert price.input_per_mtok == pytest.approx(2.5)
    assert price.output_per_mtok == pytest.approx(10.0)


# ── Unknown → None (no fabricated number) ─────────────────────────────────────


def test_unknown_cloud_model_returns_none(fake_cost_map: None) -> None:
    assert price_for("openai/does-not-exist-xyz") is None


def test_no_litellm_map_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_pricing, "_litellm_cost_map", dict)  # empty
    assert price_for("openai/gpt-4o") is None


# ── The api-side collector unions discovered + cache + preset tiers ───────────


def test_build_model_pricing_collects_and_omits(fake_cost_map: None) -> None:
    discovered = [DiscoveredModelItem(id="openai/gpt-4o", name="GPT-4o")]
    model_cache = {"g1": ["google/gemini-2.0-flash", "openai/unknown-model"]}
    presets = [ModelPreset(id="p", name="P", is_builtin=False, tiers={"big": "ollama/phi3"})]

    pricing = _build_model_pricing(discovered, model_cache, presets)

    assert pricing["openai/gpt-4o"].input_per_mtok == pytest.approx(2.5)
    assert pricing["google/gemini-2.0-flash"].local is False
    assert pricing["ollama/phi3"].local is True          # from a preset tier
    assert "openai/unknown-model" not in pricing         # unknown → omitted


# ── Real wiring smoke (tolerant of litellm data shifts) ───────────────────────


def test_real_litellm_wiring_smoke() -> None:
    cost_map = model_pricing._litellm_cost_map()
    if "gpt-4o" not in cost_map:
        pytest.skip("litellm cost map unavailable or gpt-4o renamed")
    price = price_for("openai/gpt-4o")
    assert price is not None and not price.local
    assert price.input_per_mtok > 0
