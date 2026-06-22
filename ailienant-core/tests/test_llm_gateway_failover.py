# ailienant-core/tests/test_llm_gateway_failover.py
"""Mid-session local-endpoint failover for the proxy-free BYOM call paths.

A non-OOM transport drop of a *local* endpoint fails over ONCE to the next callable
target on the capability ladder; a second failure re-raises without looping. OOM-class
drops and cloud drops are NOT failed over here (the OOM cascade owns the former).
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litellm.exceptions import APIConnectionError

from core.config.byom_config import ModelTarget
from core.config.model_resolver import get_failover_target
from tools.llm_gateway import LLMGateway, _LOCAL_LLM_TIMEOUT_S


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_target(is_local: bool, model: str, api_base: str | None = "http://localhost:11434") -> MagicMock:
    t = MagicMock()
    t.model = model
    t.api_base = api_base
    t.api_key = "" if is_local else "sk-cloud-key"
    t.is_local = is_local
    return t


def _conn_err(message: str = "connection refused") -> APIConnectionError:
    return APIConnectionError(message=message, llm_provider="ollama", model="ollama_chat/phi4")


def _oom_err() -> APIConnectionError:
    return APIConnectionError(
        message="CUDA error: out of memory on device 0", llm_provider="ollama", model="ollama_chat/phi4",
    )


def _mock_response(content: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.usage = None
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp.choices = [choice]
    return resp


def _fake_stream(deltas: List[str]) -> Any:
    """An async-iterable mimicking a litellm streaming response (no usage chunk)."""

    async def _gen() -> AsyncIterator[MagicMock]:
        for d in deltas:
            chunk = MagicMock()
            chunk.usage = None
            delta = MagicMock()
            delta.content = d
            choice = MagicMock()
            choice.delta = delta
            chunk.choices = [choice]
            yield chunk

    return _gen()


# ── Unit: get_failover_target resolution ─────────────────────────────────────


def _seed_targets(monkeypatch: Any, targets: Dict[str, ModelTarget]) -> None:
    monkeypatch.setattr("core.config.model_resolver._load", lambda: targets)


def test_failover_returns_other_tier(monkeypatch: Any) -> None:
    targets = {
        "medium": ModelTarget(model="ollama_chat/phi4", provider="ollama", api_base="http://x", is_local=True),
        "big": ModelTarget(model="ollama_chat/qwen", provider="ollama", api_base="http://y", is_local=True),
    }
    _seed_targets(monkeypatch, targets)
    nxt = get_failover_target("medium", exclude_model="ollama_chat/phi4")
    assert nxt is not None and nxt.model == "ollama_chat/qwen"


def test_failover_skips_keyless_cloud(monkeypatch: Any) -> None:
    targets = {
        "medium": ModelTarget(model="ollama_chat/phi4", provider="ollama", api_base="http://x", is_local=True),
        "cloud": ModelTarget(model="gpt-4o", provider="openai", api_key="", is_local=False),
    }
    _seed_targets(monkeypatch, targets)
    # The only neighbour is a cloud target with no key — nothing to fail over to.
    assert get_failover_target("medium", exclude_model="ollama_chat/phi4") is None


def test_failover_uses_cloud_with_key(monkeypatch: Any) -> None:
    targets = {
        "medium": ModelTarget(model="ollama_chat/phi4", provider="ollama", api_base="http://x", is_local=True),
        "cloud": ModelTarget(model="gpt-4o", provider="openai", api_key="sk-real", is_local=False),
    }
    _seed_targets(monkeypatch, targets)
    nxt = get_failover_target("medium", exclude_model="ollama_chat/phi4")
    assert nxt is not None and nxt.model == "gpt-4o"


def test_failover_none_when_empty(monkeypatch: Any) -> None:
    _seed_targets(monkeypatch, {})
    assert get_failover_target("medium", exclude_model="anything") is None


# ── acomplete_byom ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_acomplete_drop_then_recover() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    backup = _make_target(is_local=True, model="ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=[_conn_err(), _mock_response("recovered")])
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=backup), \
         patch("litellm.acompletion", new=acompletion):
        out = await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert out == "recovered"
    assert acompletion.await_count == 2  # original + one failover
    assert acompletion.await_args_list[1].kwargs.get("model") == "ollama_chat/qwen"


@pytest.mark.anyio
async def test_acomplete_persistent_drop_reraises_no_loop() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    backup = _make_target(is_local=True, model="ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=_conn_err())
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=backup), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert acompletion.await_count == 2  # original + ONE failover, then re-raise


@pytest.mark.anyio
async def test_acomplete_no_viable_failover_reraises() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    acompletion = AsyncMock(side_effect=_conn_err())
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=None), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert acompletion.await_count == 1  # no failover attempted — original surfaces


@pytest.mark.anyio
async def test_acomplete_oom_not_failed_over() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    backup = _make_target(is_local=True, model="ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=_oom_err())
    failover = MagicMock(return_value=backup)
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", new=failover), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert acompletion.await_count == 1  # OOM-class drop is not a failover trigger
    failover.assert_not_called()


@pytest.mark.anyio
async def test_acomplete_cloud_drop_not_failed_over() -> None:
    primary = _make_target(is_local=False, model="gpt-4o", api_base=None)
    failover = MagicMock()
    acompletion = AsyncMock(side_effect=_conn_err())
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", new=failover), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            await LLMGateway.acomplete_byom(messages=[{"role": "user", "content": "hi"}])
    assert acompletion.await_count == 1  # cloud target — no local failover path
    failover.assert_not_called()


# ── astream_byom ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_astream_drop_then_recover() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    backup = _make_target(is_local=True, model="ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=[_conn_err(), _fake_stream(["hel", "lo"])])
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=backup), \
         patch("litellm.acompletion", new=acompletion):
        out = [d async for d in LLMGateway.astream_byom(messages=[{"role": "user", "content": "hi"}])]
    assert "".join(out) == "hello"
    assert acompletion.await_count == 2  # one failed connect + one successful
    assert acompletion.await_args_list[1].kwargs.get("model") == "ollama_chat/qwen"


@pytest.mark.anyio
async def test_astream_persistent_drop_reraises_no_loop() -> None:
    primary = _make_target(is_local=True, model="ollama_chat/phi4")
    backup = _make_target(is_local=True, model="ollama_chat/qwen")
    acompletion = AsyncMock(side_effect=_conn_err())
    with patch("core.config.model_resolver.get_chat_target", return_value=primary), \
         patch("core.config.model_resolver.get_failover_target", return_value=backup), \
         patch("litellm.acompletion", new=acompletion):
        with pytest.raises(APIConnectionError):
            async for _ in LLMGateway.astream_byom(messages=[{"role": "user", "content": "hi"}]):
                pass
    assert acompletion.await_count == 2  # original + ONE failover connect, then re-raise
