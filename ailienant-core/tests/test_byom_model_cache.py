"""BYOM model-cache + import + preset-pool — the 8.4.9 fixes.

Certifies: canonical model-id prefixing, `_merge_patch` preserves the model cache
and derived targets across an endpoints-only save, the merged available-model pool
includes imported cloud catalogues, and built-in presets degrade cleanly with an
empty cache.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from core.config.byom_config import BYOMConfig, EndpointConfig, ModelPreset, ModelTarget
from api.byom import (
    _active_preset_tiers_changed,
    _build_builtin_presets,
    _canonical_model_id,
    _merge_patch,
)


def _ep(provider: str, eid: str = "e1", key: str = "k") -> EndpointConfig:
    return EndpointConfig(id=eid, name="t", url="", api_key=key, provider=provider)  # type: ignore[arg-type]


# ── Canonical model-id prefixing ──────────────────────────────────────────────


def test_canonical_strips_google_models_prefix() -> None:
    assert _canonical_model_id("google", "models/gemini-2.0-flash") == "google/gemini-2.0-flash"
    assert _canonical_model_id("google", "gemini-2.0-flash") == "google/gemini-2.0-flash"


def test_canonical_keeps_known_provider_prefix() -> None:
    # OpenRouter ids arrive as "vendor/model"; an already-known provider prefix is kept.
    assert _canonical_model_id("openrouter", "openrouter/meta-llama/llama-3.3-70b:free") \
        == "openrouter/meta-llama/llama-3.3-70b:free"
    # A bare vendor/model id is prefixed with the endpoint provider.
    assert _canonical_model_id("openrouter", "meta-llama/llama-3.3-70b:free") \
        == "openrouter/meta-llama/llama-3.3-70b:free"


# ── _merge_patch preserves the cache + derived targets ────────────────────────


def test_merge_patch_preserves_cache_and_targets() -> None:
    existing = BYOMConfig(
        endpoints=[_ep("google", "g1")],
        model_cache={"g1": ["google/gemini-2.0-flash"]},
        chat_models={"big": ModelTarget(model="gemini/gemini-2.0-flash", provider="google", is_local=False)},
    )
    # An endpoints-only save (no model_cache / chat_models in the body) must NOT wipe them.
    body: Dict[str, Any] = {"endpoints": [
        {"id": "g1", "name": "t", "url": "", "api_key": "sk-••••abcd", "provider": "google"}
    ]}
    merged = _merge_patch(existing, body)
    assert merged.model_cache == {"g1": ["google/gemini-2.0-flash"]}
    assert "big" in merged.chat_models
    # Masked key echoed back is replaced with the stored real key.
    assert merged.endpoints[0].api_key == "k"


def test_merge_patch_drops_cache_for_removed_endpoint() -> None:
    existing = BYOMConfig(
        endpoints=[_ep("google", "g1")],
        model_cache={"g1": ["google/gemini-2.0-flash"], "stale": ["x/y"]},
    )
    body: Dict[str, Any] = {"endpoints": [
        {"id": "g1", "name": "t", "url": "", "api_key": "k", "provider": "google"}
    ]}
    merged = _merge_patch(existing, body)
    assert "stale" not in merged.model_cache
    assert "g1" in merged.model_cache


def test_merge_patch_accepts_cache_in_body() -> None:
    existing = BYOMConfig(endpoints=[_ep("openrouter", "o1")])
    body: Dict[str, Any] = {
        "endpoints": [{"id": "o1", "name": "t", "url": "", "api_key": "k", "provider": "openrouter"}],
        "model_cache": {"o1": ["openrouter/meta-llama/llama-3.3-70b:free"]},
    }
    merged = _merge_patch(existing, body)
    assert merged.model_cache["o1"] == ["openrouter/meta-llama/llama-3.3-70b:free"]


# ── Merged pool includes imported cloud catalogues ────────────────────────────


def test_pool_includes_cached_cloud_models() -> None:
    cache = {"g1": ["google/gemini-2.0-flash", "google/gemini-1.5-pro"]}
    _presets, discovered = asyncio.run(_build_builtin_presets(cache))
    ids = {d.id for d in discovered}
    assert "google/gemini-2.0-flash" in ids
    assert "google/gemini-1.5-pro" in ids


def test_builtins_degrade_without_cache() -> None:
    presets, _discovered = asyncio.run(_build_builtin_presets({}))
    names = {p.name for p in presets}
    assert {"Local Only", "Hybrid", "Cloud Only"} <= names
    # Cloud-only template has no cloud model to point at → empty tiers, not a crash.
    cloud = next(p for p in presets if p.name == "Cloud Only")
    assert all(v == "" for v in cloud.tiers.values())


def test_cloud_template_uses_imported_model() -> None:
    cache = {"g1": ["google/gemini-2.0-flash"]}
    presets, _discovered = asyncio.run(_build_builtin_presets(cache))
    cloud = next(p for p in presets if p.name == "Cloud Only")
    assert cloud.tiers["big"] == "google/gemini-2.0-flash"


# ── Active-preset tier edits must re-derive chat_models ───────────────────────
# `tiers` is what the user picks in the UI; `chat_models` is the derived form
# every call path actually reads. A save that persists the first without
# recomputing the second strands the engine on the previous model — the panel
# shows the new choice while every turn still runs the old one.


def _preset(pid: str, tiers: Dict[str, str]) -> ModelPreset:
    return ModelPreset(id=pid, name="p", tiers=tiers)


def test_editing_the_active_presets_tiers_is_detected() -> None:
    before = BYOMConfig(
        presets=[_preset("p1", {"medium": "ollama/qwen2.5-coder:3b"})],
        active_preset_id="p1",
    )
    after = BYOMConfig(
        presets=[_preset("p1", {"medium": "google/gemini-2.5-flash"})],
        active_preset_id="p1",
    )
    assert _active_preset_tiers_changed(before, after) is True


def test_editing_an_inactive_preset_is_not_detected() -> None:
    """Only the active preset feeds chat_models — editing another one changes
    nothing the engine reads, so it must not trigger a re-apply."""
    before = BYOMConfig(
        presets=[_preset("p1", {"medium": "a"}), _preset("p2", {"medium": "b"})],
        active_preset_id="p1",
    )
    after = BYOMConfig(
        presets=[_preset("p1", {"medium": "a"}), _preset("p2", {"medium": "CHANGED"})],
        active_preset_id="p1",
    )
    assert _active_preset_tiers_changed(before, after) is False


def test_an_endpoints_only_save_is_not_detected() -> None:
    """The no-op case: same tiers in, same tiers out."""
    tiers = {"medium": "ollama/qwen2.5-coder:3b"}
    before = BYOMConfig(presets=[_preset("p1", dict(tiers))], active_preset_id="p1")
    after = BYOMConfig(presets=[_preset("p1", dict(tiers))], active_preset_id="p1")
    assert _active_preset_tiers_changed(before, after) is False


def test_no_active_preset_is_not_detected() -> None:
    before = BYOMConfig(presets=[_preset("p1", {"medium": "a"})])
    after = BYOMConfig(presets=[_preset("p1", {"medium": "b"})])
    assert _active_preset_tiers_changed(before, after) is False


def test_a_newly_created_active_preset_is_detected() -> None:
    """Activating a preset that did not exist in the previous config still needs
    chat_models derived — there is no prior tier map to compare against."""
    before = BYOMConfig(presets=[], active_preset_id="p1")
    after = BYOMConfig(presets=[_preset("p1", {"medium": "a"})], active_preset_id="p1")
    assert _active_preset_tiers_changed(before, after) is True
