"""Capability-gated streaming structured output (DEBT-013).

On the native-thinking streaming branch, ``response_format`` (provider-enforced
JSON mode) is preserved where the provider supports it and dropped where it does
not, with the ADR-742 sanitizer recovering clean JSON in either case:

  A — capable provider (OpenAI) forwards ``response_format`` to the stream.
  B — incapable provider (Anthropic) does NOT forward it; the sanitizer still
      strips a markdown fence so the returned answer is clean JSON.
  C — a backend that rejects ``response_format`` mid-setup degrades once
      (memoized) and re-runs the stream without it, before any chunk is read.
  D — a model already memoized as unsupported is never re-sent the param.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import patch

import pytest

import tools.llm_gateway as gw
from tools.llm_gateway import LLMGateway
from tools.stream_delta import StreamDelta

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _target(model: str, provider: str) -> Any:
    from core.config.byom_config import ModelTarget

    return ModelTarget(
        model=model, provider=provider, api_base=None, api_key="sk-test", is_local=False,
    )


def _recording_stream(captured: Dict[str, Any], *deltas: StreamDelta) -> Any:
    """A fake ``astream_byom_thinking`` that records its kwargs and yields deltas."""

    def _factory(*_a: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
        captured.update(kw)

        async def _gen() -> AsyncIterator[StreamDelta]:
            for d in deltas:
                yield d

        return _gen()

    return _factory


async def _noop_sink(_text: str) -> None:
    return None


# ── A — capable provider forwards response_format ─────────────────────────────


async def test_a_openai_forwards_response_format_to_stream() -> None:
    captured: Dict[str, Any] = {}
    rf = {"type": "json_object"}
    with patch("core.config.model_resolver.get_chat_target", return_value=_target("o1-preview", "openai")), \
         patch.object(LLMGateway, "astream_byom_thinking",
                      new=_recording_stream(captured, StreamDelta("text", '{"ok": 1}'))):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format=rf,
            on_thinking=_noop_sink,
            enable_thinking=True,
        )
    assert captured.get("response_format") == rf  # provider-enforced JSON kept
    assert out == '{"ok": 1}'


# ── B — incapable provider drops it; sanitizer still cleans the answer ─────────


async def test_b_anthropic_drops_response_format_sanitizer_recovers() -> None:
    captured: Dict[str, Any] = {}
    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet", "anthropic")), \
         patch.object(LLMGateway, "astream_byom_thinking",
                      new=_recording_stream(
                          captured,
                          StreamDelta("text", "```json\n"),
                          StreamDelta("text", '{"ok": 2}'),
                          StreamDelta("text", "\n```"),
                      )):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=_noop_sink,
            enable_thinking=True,
        )
    assert captured.get("response_format") is None  # never sent to Anthropic
    assert out == '{"ok": 2}'  # fence stripped by the sanitizer fallback


# ── C / D — self-healing degrade inside astream_byom_thinking ─────────────────


def _chunk(content: str) -> Any:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=None, content=content))],
    )


def _async_iter(items: List[Any]) -> AsyncIterator[Any]:
    async def _gen() -> AsyncIterator[Any]:
        for it in items:
            yield it

    return _gen()


async def test_c_rejection_degrades_and_memoizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "_RESPONSE_FORMAT_UNSUPPORTED", set())
    calls: List[Dict[str, Any]] = []

    async def _acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            raise ValueError("Invalid request: response_format is not supported by this model")
        return _async_iter([_chunk('{"ok": 3}')])

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("o1-preview", "openai")), \
         patch.object(gw.litellm, "acompletion", side_effect=_acompletion):
        out: List[str] = []
        async for delta in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}],
            tier="big",
            response_format={"type": "json_object"},
        ):
            out.append(delta.text)

    assert "".join(out) == '{"ok": 3}'
    assert len(calls) == 2  # rejected once, retried once
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    assert "o1-preview" in gw._RESPONSE_FORMAT_UNSUPPORTED  # memoized for the session


async def test_d_memoized_model_never_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "_RESPONSE_FORMAT_UNSUPPORTED", {"o1-preview"})
    calls: List[Dict[str, Any]] = []

    async def _acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _async_iter([_chunk('{"ok": 4}')])

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("o1-preview", "openai")), \
         patch.object(gw.litellm, "acompletion", side_effect=_acompletion):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}],
            tier="big",
            response_format={"type": "json_object"},
        ):
            pass

    assert len(calls) == 1  # no failed round-trip
    assert "response_format" not in calls[0]  # pre-emptively stripped
