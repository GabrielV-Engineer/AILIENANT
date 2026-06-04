# tests/test_response_format_degradation.py
"""Graceful degradation when a backend rejects the response_format JSON-mode param.

A backend that 400s on response_format must not kill the agent turn: the gateway
strips the param, re-emits once, and the caller's existing JSON repair recovers
the output. A backend that accepts it (cloud, capable local) is untouched — no
extra round-trip. Incompatible backends are memoed so the failed round-trip is
paid at most once per session.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import litellm
from litellm import ModelResponse
from litellm.exceptions import ContextWindowExceededError

from tools.llm_gateway import LLMGateway, _RESPONSE_FORMAT_UNSUPPORTED

_RF: dict[str, str] = {"type": "json_object"}
_MODEL: str = "test-provider/test-model"


@pytest.fixture(autouse=True)
def _clear_memo() -> Any:
    """The unsupported-model memo is a module global — isolate it per test."""
    _RESPONSE_FORMAT_UNSUPPORTED.clear()
    yield
    _RESPONSE_FORMAT_UNSUPPORTED.clear()


def _mock_response() -> MagicMock:
    resp = MagicMock()
    resp.usage = None  # skip token accounting
    resp.choices = [MagicMock()]
    return resp


def _rf_rejecting(**kwargs: Any) -> MagicMock:
    """Stub: 400 when response_format is present, succeed when it is absent."""
    if "response_format" in kwargs:
        raise Exception("BadRequestError: 'response_format' is not supported by this model")
    return _mock_response()


def _rf_only_kwargs(mock: AsyncMock | MagicMock) -> list[bool]:
    """Per-call booleans: did this call carry response_format?"""
    return ["response_format" in c.kwargs for c in mock.call_args_list]


# ── ainvoke (async) ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ainvoke_reject_then_recover() -> None:
    """First call carries response_format → 400 → retry without it → success + memo."""
    mock = AsyncMock(side_effect=_rf_rejecting)
    with patch("litellm.acompletion", new=mock):
        resp = await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert isinstance(resp, MagicMock)            # the recovered (param-less) response
    assert mock.await_count == 2                  # failed attempt + one retry
    assert _rf_only_kwargs(mock) == [True, False]  # sent, then stripped
    assert _MODEL in _RESPONSE_FORMAT_UNSUPPORTED  # learned


@pytest.mark.anyio
async def test_ainvoke_adaptive_skip_after_memo() -> None:
    """A model already known to reject the param is stripped pre-emptively (1 call)."""
    _RESPONSE_FORMAT_UNSUPPORTED.add(_MODEL)
    mock = AsyncMock(side_effect=_rf_rejecting)
    with patch("litellm.acompletion", new=mock):
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert mock.await_count == 1                   # no wasted failed round-trip
    assert _rf_only_kwargs(mock) == [False]


@pytest.mark.anyio
async def test_ainvoke_capable_backend_unchanged() -> None:
    """A backend that accepts response_format gets it, once, with no retry or memo."""
    mock = AsyncMock(return_value=_mock_response())
    with patch("litellm.acompletion", new=mock):
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert mock.await_count == 1
    assert _rf_only_kwargs(mock) == [True]
    assert _MODEL not in _RESPONSE_FORMAT_UNSUPPORTED


@pytest.mark.anyio
async def test_ainvoke_unrelated_error_propagates() -> None:
    """A non-response_format error is not swallowed by the degradation branch."""
    mock = AsyncMock(side_effect=RuntimeError("connection reset by peer"))
    with patch("litellm.acompletion", new=mock):
        with pytest.raises(RuntimeError, match="connection reset"):
            await LLMGateway.ainvoke(
                messages=[{"role": "user", "content": "hi"}],
                model=_MODEL,
                response_format=_RF,
            )
    assert mock.await_count == 1
    assert _MODEL not in _RESPONSE_FORMAT_UNSUPPORTED


@pytest.mark.anyio
async def test_ainvoke_oom_still_cascades_not_rf_retry() -> None:
    """A ContextWindowExceededError must route to the OOM cascade, not the rf-retry."""
    sentinel = ModelResponse()
    cascade = AsyncMock(return_value=sentinel)
    ctx_err = ContextWindowExceededError(
        message="context window exceeded", model=_MODEL, llm_provider="ollama"
    )
    mock = AsyncMock(side_effect=ctx_err)
    with patch("litellm.acompletion", new=mock), \
         patch("tools.llm_gateway._oom_cascade", new=cascade):
        resp = await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert resp is sentinel
    cascade.assert_awaited_once()
    assert mock.await_count == 1                    # no rf-retry fired
    assert _MODEL not in _RESPONSE_FORMAT_UNSUPPORTED


# ── invoke (sync) ────────────────────────────────────────────────────────────


def test_invoke_reject_then_recover() -> None:
    """Sync parity: 400 on response_format → strip + retry once → success + memo."""
    mock = MagicMock(side_effect=_rf_rejecting)
    with patch("litellm.completion", new=mock):
        LLMGateway.invoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert mock.call_count == 2
    assert _rf_only_kwargs(mock) == [True, False]
    assert _MODEL in _RESPONSE_FORMAT_UNSUPPORTED


def test_invoke_capable_backend_unchanged() -> None:
    """Sync: a capable backend gets the param once, no retry, no memo."""
    mock = MagicMock(return_value=_mock_response())
    with patch("litellm.completion", new=mock):
        LLMGateway.invoke(
            messages=[{"role": "user", "content": "hi"}],
            model=_MODEL,
            response_format=_RF,
        )
    assert mock.call_count == 1
    assert _rf_only_kwargs(mock) == [True]
    assert _MODEL not in _RESPONSE_FORMAT_UNSUPPORTED
