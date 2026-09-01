# ailienant-core/tests/test_model_resolver.py
"""Phase 7.9.B.17 — chat-target resolution + Ollama route normalization.

get_chat_target must rewrite `ollama/<m>` to `ollama_chat/<m>` (litellm chat
endpoint, applies the model template) at read time, so already-persisted presets
are fixed without a re-apply. Non-ollama targets are untouched. Tier fallback
still works and is normalized too.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from core.config import model_resolver
from core.config.byom_config import ModelTarget


def _cfg(chat_models: dict) -> SimpleNamespace:
    return SimpleNamespace(chat_models=chat_models)


def _ollama(model: str) -> ModelTarget:
    return ModelTarget(
        model=model, provider="ollama",
        api_base="http://localhost:11434", api_key="", is_local=True,
    )


def test_get_chat_target_normalizes_ollama_to_ollama_chat() -> None:
    targets = {"medium": _ollama("ollama/llama3.1")}
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "ollama_chat/llama3.1"
    assert t.provider == "ollama"


def test_get_chat_target_leaves_non_ollama_untouched() -> None:
    targets = {
        "medium": ModelTarget(
            model="gpt-4o", provider="openai", api_base=None, api_key="sk-x", is_local=False
        )
    }
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "gpt-4o"


def test_get_chat_target_falls_back_across_tiers_and_normalizes() -> None:
    # Request "big" but only "small" exists → fallback + normalize.
    targets = {"small": _ollama("ollama/phi3")}
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("big")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "ollama_chat/phi3"


def test_get_chat_target_none_when_no_preset() -> None:
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg({})):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is None


# ─────────────────────────────────────────────────────────────────────────
# probe_runtime_capabilities — the real window and reasoning capability come
# from the runtime, never a hardcoded name-substring guess or a declared
# LLMProfile. Live-verified separately against a real Ollama instance
# (gemma4:e4b: context_length=131072, supports_thinking=True, matching a
# direct measurement of the served model); these tests are hermetic against a
# mocked transport, per the mock-vs-integration split in the charter's test
# taxonomy.
# ─────────────────────────────────────────────────────────────────────────

# Captured BEFORE any test patches httpx.AsyncClient: the fake client factories
# below must construct a REAL AsyncClient (bound to a mock transport) rather
# than recursing into the patched name — `patch("httpx.AsyncClient", ...)`
# replaces the name globally, so a factory that calls `httpx.AsyncClient(...)`
# by name would call itself.
_RealAsyncClient = httpx.AsyncClient


def _mock_show_transport(payload: dict, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def _client_factory(transport: httpx.MockTransport):
    return lambda **_: _RealAsyncClient(transport=transport)


@pytest.mark.anyio
async def test_probe_runtime_capabilities_reads_context_length_and_thinking() -> None:
    model_resolver.refresh()
    payload = {
        "model_info": {"gemma4.context_length": 131072, "gemma4.embedding_length": 3072},
        "capabilities": ["completion", "tools", "thinking"],
    }
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(payload))):
        caps = await model_resolver.probe_runtime_capabilities(_ollama("ollama_chat/gemma4:e4b"))
    model_resolver.refresh()
    assert caps.context_length == 131072
    assert caps.supports_thinking is True


@pytest.mark.anyio
async def test_probe_runtime_capabilities_false_when_thinking_not_declared() -> None:
    model_resolver.refresh()
    payload = {
        "model_info": {"qwen2.context_length": 32768},
        "capabilities": ["completion", "tools", "insert"],
    }
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(payload))):
        caps = await model_resolver.probe_runtime_capabilities(_ollama("ollama_chat/qwen2.5-coder:3b"))
    model_resolver.refresh()
    assert caps.context_length == 32768
    assert caps.supports_thinking is False


@pytest.mark.anyio
async def test_probe_runtime_capabilities_non_ollama_returns_unknown_without_a_call() -> None:
    """A cloud/non-Ollama target has no /api/show equivalent here — must return
    UNKNOWN rather than guess, and must not attempt any network call."""
    model_resolver.refresh()
    target = ModelTarget(model="gpt-4o", provider="openai", api_base=None, is_local=False)
    with patch("httpx.AsyncClient") as mock_client:
        caps = await model_resolver.probe_runtime_capabilities(target)
    model_resolver.refresh()
    mock_client.assert_not_called()
    assert caps.context_length is None
    assert caps.supports_thinking is False


@pytest.mark.anyio
async def test_probe_runtime_capabilities_degrades_to_unknown_on_transport_failure() -> None:
    """A probe fault (timeout, connection refused, malformed JSON) must degrade
    to UNKNOWN — never raise and never block the caller's chat turn."""
    model_resolver.refresh()

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with patch("httpx.AsyncClient", _client_factory(httpx.MockTransport(_raise))):
        caps = await model_resolver.probe_runtime_capabilities(_ollama("ollama_chat/gemma4:e4b"))
    model_resolver.refresh()
    assert caps.context_length is None
    assert caps.supports_thinking is False


@pytest.mark.anyio
async def test_probe_runtime_capabilities_caches_per_bare_model_name() -> None:
    """Two tiers pointing at the SAME physical model (a real, observed AILIENANT
    configuration — N5) must share one probe, not issue it once per tier."""
    model_resolver.refresh()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"model_info": {"gemma4.context_length": 131072}, "capabilities": ["thinking"]})

    with patch("httpx.AsyncClient", _client_factory(httpx.MockTransport(handler))):
        medium = _ollama("ollama_chat/gemma4:e4b")
        big = _ollama("ollama_chat/gemma4:e4b")
        await model_resolver.probe_runtime_capabilities(medium)
        await model_resolver.probe_runtime_capabilities(big)
    model_resolver.refresh()
    assert call_count == 1


@pytest.mark.anyio
async def test_probe_runtime_capabilities_strips_ollama_chat_prefix_for_the_show_call() -> None:
    model_resolver.refresh()
    seen_model_names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen_model_names.append(_json.loads(request.content)["model"])
        return httpx.Response(200, json={"model_info": {}, "capabilities": []})

    with patch("httpx.AsyncClient", _client_factory(httpx.MockTransport(handler))):
        await model_resolver.probe_runtime_capabilities(_ollama("ollama_chat/gemma4:e4b"))
    model_resolver.refresh()
    assert seen_model_names == ["gemma4:e4b"]


# ─────────────────────────────────────────────────────────────────────────
# resolve_num_ctx — every local Ollama call was pinned to Ollama's own silent
# 4096-token default because nothing in AILIENANT ever set the parameter, even
# though the model's real architectural capacity (probed above) was routinely
# far larger. Live-verified separately against a real Ollama instance
# (gemma4:e4b, 131072 architectural max, RAM headroom): loading at 16384
# grew the resident model 9.43 -> 9.6 GB, the +0.2 GB figure the RAM-estimate
# constant below is derived from. These tests are hermetic against a mocked
# transport and a mocked hardware reading.
# ─────────────────────────────────────────────────────────────────────────


def _show_payload(context_length: int) -> dict:
    return {"model_info": {"gemma4.context_length": context_length}, "capabilities": ["thinking"]}


def _patch_ample_ram():
    """Report abundant free RAM so the RAM-affordability clamp never binds —
    isolates tests that are really about the architectural/hard-ceiling clamps."""
    from shared.hardware import HardwareProfile

    profile = HardwareProfile(
        os_type="linux", is_apple_silicon=False,
        ram_gb=64.0, ram_available_gb=32.0, vram_gb=0.0, vram_used_gb=0.0,
        gpu_name=None, cpu_name="test-cpu", cpu_cores=8, cpu_freq_mhz=3000.0,
    )
    return patch("shared.hardware.HardwareDetector.detect", return_value=profile)


@pytest.mark.anyio
async def test_resolve_num_ctx_never_below_the_ollama_default() -> None:
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(131_072)))), \
         _patch_ample_ram():
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=500)
    model_resolver.refresh()
    assert n == model_resolver._OLLAMA_DEFAULT_NUM_CTX


@pytest.mark.anyio
async def test_resolve_num_ctx_grows_to_cover_a_real_requirement() -> None:
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(131_072)))), \
         _patch_ample_ram():
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=6000)
    model_resolver.refresh()
    assert n == 6000


@pytest.mark.anyio
async def test_resolve_num_ctx_never_exceeds_the_architectural_max() -> None:
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(8192)))), \
         _patch_ample_ram():
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/tiny-model"), min_required=100_000)
    model_resolver.refresh()
    assert n == 8192


@pytest.mark.anyio
async def test_resolve_num_ctx_respects_the_policy_hard_ceiling() -> None:
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(131_072)))), \
         _patch_ample_ram():
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=100_000)
    model_resolver.refresh()
    assert n == model_resolver._NUM_CTX_HARD_CEILING


@pytest.mark.anyio
async def test_resolve_num_ctx_never_shrinks_an_already_cached_value() -> None:
    """Ollama reloads the whole model when num_ctx changes — a later call
    needing LESS than what is already cached must reuse the cached value, not
    force a pointless reload down."""
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(131_072)))), \
         _patch_ample_ram():
        first = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=20_000)
        second = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=500)
    model_resolver.refresh()
    assert second == first == 20_000


@pytest.mark.anyio
async def test_resolve_num_ctx_returns_none_for_a_non_ollama_target() -> None:
    model_resolver.refresh()
    target = ModelTarget(model="gpt-4o", provider="openai", api_base=None, is_local=False)
    n = await model_resolver.resolve_num_ctx(target, min_required=6000)
    model_resolver.refresh()
    assert n is None


@pytest.mark.anyio
async def test_resolve_num_ctx_returns_none_when_the_architectural_max_is_unknown() -> None:
    """A probe failure (or a model with no reported context_length) must
    degrade to omitting num_ctx entirely — never a guessed number."""
    model_resolver.refresh()

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with patch("httpx.AsyncClient", _client_factory(httpx.MockTransport(_raise))):
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=6000)
    model_resolver.refresh()
    assert n is None


@pytest.mark.anyio
async def test_resolve_num_ctx_falls_back_to_the_architectural_ceiling_when_ram_read_fails() -> None:
    """A hardware-detection fault must not block resolution — the architectural
    and policy ceilings still apply on their own."""
    model_resolver.refresh()
    with patch("httpx.AsyncClient", _client_factory(_mock_show_transport(_show_payload(8192)))), \
         patch("shared.hardware.HardwareDetector.detect", side_effect=RuntimeError("boom")):
        n = await model_resolver.resolve_num_ctx(_ollama("ollama_chat/gemma4:e4b"), min_required=6000)
    model_resolver.refresh()
    assert n == 6000


# ─────────────────────────────────────────────────────────────────────────
# check_local_admission — a hard pre-flight gate distinct from resolve_num_ctx's
# WINDOW SIZING above: even a correctly-sized num_ctx can still be a call this
# machine cannot currently afford to run at all (another process ate the RAM
# since the last reading, or this is a resize that forces a full model
# reload). Refuses outright rather than letting the OS silently thrash.
# ─────────────────────────────────────────────────────────────────────────


def _ram_profile(available_gb: float) -> "object":
    from shared.hardware import HardwareProfile

    return HardwareProfile(
        os_type="linux", is_apple_silicon=False,
        ram_gb=64.0, ram_available_gb=available_gb, vram_gb=0.0, vram_used_gb=0.0,
        gpu_name=None, cpu_name="test-cpu", cpu_cores=8, cpu_freq_mhz=3000.0,
    )


def _mock_ps_transport(models: list[dict], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"models": models})
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_check_local_admission_admits_when_ram_is_ample() -> None:
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(32.0)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport([]))):
        await model_resolver.check_local_admission(_ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192)
    # No exception raised = admitted.


@pytest.mark.anyio
async def test_check_local_admission_refuses_when_ram_is_tight() -> None:
    # 8192 tokens * 14_500 bytes/token (~113 MB) against ~50 MB free must refuse.
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(0.05)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport([]))):
        with pytest.raises(model_resolver.LocalResourceExhaustedError, match="Not enough free memory"):
            await model_resolver.check_local_admission(
                _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
            )


@pytest.mark.anyio
async def test_check_local_admission_adds_reload_headroom_for_an_already_resident_model() -> None:
    """A resize that forces Ollama to reload the whole model needs headroom for
    BOTH the outgoing and incoming copies transiently. Calibrated so the base
    projection ALONE (~0.11 GB) comfortably admits against 1.5 GB free — the
    raise below is proof the resident model's 1 GB was actually added, not an
    artifact of an already-tight baseline."""
    resident_models = [
        {"name": "gemma4:e4b", "size": 1024 * 1024 * 1024, "context_length": 2048},
    ]
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(1.5)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport(resident_models))):
        with pytest.raises(model_resolver.LocalResourceExhaustedError):
            await model_resolver.check_local_admission(
                _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
            )


@pytest.mark.anyio
async def test_check_local_admission_charges_no_reload_when_resident_window_already_fits() -> None:
    """A model resident at a window that already holds the request serves it
    without reloading, so its resident bytes must NOT be charged. Same 1.5 GB /
    1 GB pairing as the sibling above — charging them here would refuse a model
    that is loaded, warm, and immediately servable, which is the inversion this
    gate exists to prevent."""
    resident_models = [
        {"name": "gemma4:e4b", "size": 1024 * 1024 * 1024, "context_length": 16384},
    ]
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(1.5)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport(resident_models))):
        await model_resolver.check_local_admission(
            _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
        )
    # No exception — a no-reload serve never pays the resident model's footprint.


@pytest.mark.anyio
async def test_check_local_admission_assumes_reload_when_context_length_is_absent() -> None:
    """Older Ollama builds omit `context_length` from /api/ps. With no way to
    tell whether a reload is coming, the gate must over-reserve: refusing a call
    it was unsure about is recoverable, letting the box swap is not."""
    resident_models = [{"name": "gemma4:e4b", "size": 1024 * 1024 * 1024}]
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(1.5)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport(resident_models))):
        with pytest.raises(model_resolver.LocalResourceExhaustedError):
            await model_resolver.check_local_admission(
                _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
            )


@pytest.mark.anyio
async def test_check_local_admission_ignores_a_different_resident_model() -> None:
    """Reload headroom only applies to THIS call's own target model — another
    model's residency must not inflate this call's projected footprint. Same
    1.5 GB / 1 GB pairing as the sibling test above: if the model-name match
    were ever wrongly loosened, this would start raising too."""
    resident_models = [{"name": "some-other-model:7b", "size": 1024 * 1024 * 1024}]
    with patch("shared.hardware.HardwareDetector.detect", return_value=_ram_profile(1.5)), \
         patch("httpx.AsyncClient", _client_factory(_mock_ps_transport(resident_models))):
        await model_resolver.check_local_admission(
            _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
        )
    # No exception — the other model's 1 GB never entered this call's projection.


@pytest.mark.anyio
async def test_check_local_admission_is_noop_for_cloud_target() -> None:
    """A cloud target has no local RAM constraint — must never even read
    hardware, let alone refuse, regardless of how tight a mocked reading is."""
    target = ModelTarget(model="gpt-4o", provider="openai", api_base=None, is_local=False)
    with patch("shared.hardware.HardwareDetector.detect") as mock_detect:
        await model_resolver.check_local_admission(target, requested_num_ctx=1_000_000)
    mock_detect.assert_not_called()


@pytest.mark.anyio
async def test_check_local_admission_degrades_to_admit_on_hardware_read_failure() -> None:
    """An unreadable hardware signal must not block a call — matches every
    other best-effort probe's degrade-on-failure contract in this module."""
    with patch("shared.hardware.HardwareDetector.detect", side_effect=RuntimeError("boom")):
        await model_resolver.check_local_admission(
            _ollama("ollama_chat/gemma4:e4b"), requested_num_ctx=8192,
        )
    # No exception raised = degraded to admit.


# ── resolve_declared_window ──────────────────────────────────────────────────
# probe_runtime_capabilities speaks only to Ollama, so before this existed every
# non-Ollama target fell straight through to a flat conservative constant — a
# cloud model with a million-token window was budgeted at 8192, which truncated a
# structured-output call mid-object and surfaced as a schema error.


def test_resolve_declared_window_reads_a_known_cloud_model() -> None:
    window = model_resolver.resolve_declared_window("gemini/gemini-2.5-flash")
    assert window is not None and window > 100_000


def test_resolve_declared_window_returns_none_for_an_unknown_model() -> None:
    assert model_resolver.resolve_declared_window("not-a-real/model-xyz") is None


def test_resolve_declared_window_returns_none_for_an_empty_name() -> None:
    assert model_resolver.resolve_declared_window("") is None
    assert model_resolver.resolve_declared_window(None) is None
