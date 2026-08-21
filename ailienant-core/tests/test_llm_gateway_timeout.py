# ailienant-core/tests/test_llm_gateway_timeout.py
"""Local BYOM models receive a timeout scaled to their own output ceiling, not a
flat guess (DEBT-191) — plus the per-model adaptive calibration layer built on
top of that static formula."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.llm_gateway import (
    LLMGateway,
    resolve_local_timeout,
    _record_local_completion,
    _measured_local_seconds_per_token,
    _local_model_completions,
    _LOCAL_LLM_SECONDS_PER_TOKEN,
    _LOCAL_LLM_TIMEOUT_CUSHION_S,
    _LOCAL_LLM_TIMEOUT_FLOOR_S,
    _LOCAL_RATE_MIN_SAMPLES,
    _LOCAL_RATE_MIN_TOTAL_TOKENS,
    _LOCAL_RATE_SAFETY_MARGIN,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_local_calibration():
    """`_local_model_completions` is a module-level dict shared across the whole
    test session (mirrors `_companion_emission_counts`'s own autouse fixture in
    test_coder_companion.py) — reset it per test so one test's calibration data
    never leaks into another's timeout expectations, regardless of run order."""
    _local_model_completions.clear()
    yield
    _local_model_completions.clear()


def _make_target(is_local: bool, model: str = "ollama_chat/phi4") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.api_base = "http://localhost:11434"
    t.api_key = None
    t.is_local = is_local
    return t


def _mock_response(completion_tokens: int = 0) -> MagicMock:
    resp = MagicMock()
    if completion_tokens > 0:
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=completion_tokens)
    else:
        resp.usage = None
    resp.choices = [MagicMock()]
    return resp


# ─── ainvoke/acomplete_byom wiring — local target gets the scaled timeout ─────


async def test_ainvoke_local_byom_uses_scaled_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    assert mock_litellm.await_args is not None
    # ainvoke's own default max_tokens=4096, no calibration data yet — static formula.
    assert mock_litellm.await_args.kwargs.get("timeout") == resolve_local_timeout(4096)


async def test_ainvoke_cloud_byom_keeps_caller_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=False, model="claude-haiku-4-5-20251001")), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
            timeout=60.0,
        )
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("timeout") == 60.0


async def test_acomplete_byom_local_uses_scaled_timeout() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.acomplete_byom(
            messages=[{"role": "user", "content": "hi"}],
        )
    assert mock_litellm.await_args is not None
    # acomplete_byom's own default max_tokens=1024, no calibration data yet.
    assert mock_litellm.await_args.kwargs.get("timeout") == resolve_local_timeout(1024)


async def test_ainvoke_local_byom_uses_reduced_retries() -> None:
    """DEBT-191 follow-up: a local target's timeout means slow-or-dead hardware,
    not a transient network blip — retrying re-runs the same slow generation for
    no benefit, so a local target gets fewer transport retries than cloud."""
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    from tools.llm_gateway import _LOCAL_LLM_MAX_RETRIES
    from brain.retry_policy import LLM_MAX_TRANSPORT_RETRIES
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("max_retries") == _LOCAL_LLM_MAX_RETRIES
    assert _LOCAL_LLM_MAX_RETRIES < LLM_MAX_TRANSPORT_RETRIES


async def test_ainvoke_cloud_byom_keeps_standard_retries() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=False, model="claude-haiku-4-5-20251001")), \
         patch("litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock_litellm:
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
        )
    from brain.retry_policy import LLM_MAX_TRANSPORT_RETRIES
    assert mock_litellm.await_args is not None
    assert mock_litellm.await_args.kwargs.get("max_retries") == LLM_MAX_TRANSPORT_RETRIES


# ─── resolve_local_timeout — static formula (no calibration) ─────────────────


def test_resolve_local_timeout_matches_static_formula_with_no_calibration() -> None:
    for max_tokens in (100, 1024, 4096, 16384):
        expected = max(
            _LOCAL_LLM_TIMEOUT_FLOOR_S,
            max_tokens * _LOCAL_LLM_SECONDS_PER_TOKEN + _LOCAL_LLM_TIMEOUT_CUSHION_S,
        )
        assert resolve_local_timeout(max_tokens) == expected


def test_resolve_local_timeout_never_below_the_floor() -> None:
    assert resolve_local_timeout(1) == _LOCAL_LLM_TIMEOUT_FLOOR_S
    assert resolve_local_timeout(0) == _LOCAL_LLM_TIMEOUT_FLOOR_S


# ─── _record_local_completion — degenerate samples are skipped ───────────────


def test_record_local_completion_skips_zero_tokens() -> None:
    _record_local_completion("m", 0, 5.0)
    assert "m" not in _local_model_completions


def test_record_local_completion_skips_nonpositive_duration() -> None:
    _record_local_completion("m", 50, 0.0)
    _record_local_completion("m", 50, -1.0)
    assert "m" not in _local_model_completions


# ─── _measured_local_seconds_per_token — the two-axis trust gate ─────────────


def test_measured_rate_is_none_below_min_samples() -> None:
    # One huge sample — plenty of total tokens, but a single sample never meets
    # _LOCAL_RATE_MIN_SAMPLES on its own.
    assert _LOCAL_RATE_MIN_SAMPLES >= 2
    _record_local_completion("m", 10_000, 100.0)
    assert _measured_local_seconds_per_token("m") is None


def test_measured_rate_is_none_below_min_total_tokens() -> None:
    # Two tiny samples — this exact shape produced a wildly inflated multi-hour
    # timeout estimate when live-tested (fixed per-request overhead dominating a
    # near-zero completion), which is why this gate exists.
    _record_local_completion("m", 3, 11.0)
    _record_local_completion("m", 3, 3.0)
    assert sum(t for t, _ in _local_model_completions["m"]) < _LOCAL_RATE_MIN_TOTAL_TOKENS
    assert _measured_local_seconds_per_token("m") is None


def test_measured_rate_kicks_in_once_both_gates_are_satisfied() -> None:
    _record_local_completion("m", 200, 20.0)
    _record_local_completion("m", 200, 20.0)
    total_tokens = 400
    total_duration = 40.0
    assert total_tokens >= _LOCAL_RATE_MIN_TOTAL_TOKENS
    assert _measured_local_seconds_per_token("m") == total_duration / total_tokens


def test_resolve_local_timeout_uses_calibrated_rate_not_the_static_assumption() -> None:
    model = "ollama_chat/qwen2.5-coder:3b"
    _record_local_completion(model, 200, 20.0)
    _record_local_completion(model, 200, 20.0)
    measured = 40.0 / 400  # 0.1 s/token — well below the static 0.5 s/token default
    expected = max(
        _LOCAL_LLM_TIMEOUT_FLOOR_S,
        4096 * (measured * _LOCAL_RATE_SAFETY_MARGIN) + _LOCAL_LLM_TIMEOUT_CUSHION_S,
    )
    actual = resolve_local_timeout(4096, model)
    assert actual == expected
    # And it genuinely differs from the uncalibrated static value for this input.
    assert actual != resolve_local_timeout(4096)


def test_calibration_is_isolated_per_model() -> None:
    calibrated = "ollama_chat/qwen2.5-coder:3b"
    uncalibrated = "ollama_chat/gemma4:e4b"
    _record_local_completion(calibrated, 200, 20.0)
    _record_local_completion(calibrated, 200, 20.0)
    assert _measured_local_seconds_per_token(uncalibrated) is None
    assert resolve_local_timeout(4096, uncalibrated) == resolve_local_timeout(4096)
    assert resolve_local_timeout(4096, calibrated) != resolve_local_timeout(4096)


def test_floor_wins_even_over_a_suspiciously_fast_calibrated_rate() -> None:
    model = "m"
    # 10,000 tokens in 1 second — an implausibly fast rate a bad/noisy sample
    # could still produce; the floor must not collapse to near-zero because of it.
    _record_local_completion(model, 10_000, 1.0)
    _record_local_completion(model, 10_000, 1.0)
    assert resolve_local_timeout(100, model) == _LOCAL_LLM_TIMEOUT_FLOOR_S


# ─── Retry-path timing fix — a calibration sample reflects only the call that
# actually produced its completion_tokens, not a wasted first attempt ─────────


async def test_response_format_retry_records_only_the_successful_attempts_time() -> None:
    import asyncio

    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(0.3)
            raise Exception("this backend does not support response_format")
        await asyncio.sleep(0.05)
        return _mock_response(completion_tokens=250)

    with patch("core.config.model_resolver.get_chat_target", return_value=_make_target(is_local=True)), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_side_effect)):
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model="ailienant/medium",
            response_format={"type": "json_object"},
        )

    assert call_count == 2
    history = _local_model_completions.get("ollama_chat/phi4")
    assert history is not None and len(history) == 1
    recorded_tokens, recorded_duration = history[0]
    assert recorded_tokens == 250
    # Well under the first attempt's 0.3s sleep, close to the second's 0.05s —
    # proves the wasted first attempt's time was excluded, not included.
    assert recorded_duration < 0.2
