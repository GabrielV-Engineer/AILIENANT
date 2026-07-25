"""Tests for the shared reasoning engine and the simulated-reasoning fallback.

Covers ``LLMGateway.astream_reasoning`` (native vs simulated selection), the
code-free scaffold injection (no caller mutation), and
``acomplete_with_thinking`` end-to-end on a non-native model (simulated trace +
clean, JSON-sanitized answer, answer not starved by the reasoning block).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List, Tuple

import pytest

from tools.llm_gateway import LLMGateway
from tools.stream_delta import StreamDelta

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeTarget:
    def __init__(self, model: str) -> None:
        self.model = model
        self.is_local = True
        self.api_base = "http://local"


def _patch_target(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setattr(
        "core.config.model_resolver.get_chat_target",
        lambda tier: _FakeTarget(model),
    )


def _fake_flat_stream(chunks: List[str], captured: dict[str, Any] | None = None):
    async def _gen(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        if captured is not None:
            captured["messages"] = messages
            captured["kwargs"] = kwargs
        for c in chunks:
            yield c
    return _gen


async def _collect(agen: AsyncIterator[StreamDelta]) -> Tuple[str, str, set[str]]:
    think: List[str] = []
    answer: List[str] = []
    sources: set[str] = set()
    async for d in agen:
        if d.kind == "thinking":
            think.append(d.text)
            sources.add(d.source)
        else:
            answer.append(d.text)
    return "".join(think), "".join(answer), sources


async def test_astream_reasoning_simulated_on_non_native_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_target(monkeypatch, "llama3.1")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "astream_byom",
        staticmethod(_fake_flat_stream(
            ["<thinking>", "I weigh the trade", "-offs", "</thinking>", "the answer"],
            captured,
        )),
    )
    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning([{"role": "user", "content": "hi"}], tier="medium")
    )
    assert think == "I weigh the trade-offs"
    assert answer == "the answer"
    assert sources == {"simulated"}
    # The flat stream saw a scaffolded system message, not the bare user turn.
    sent = captured["messages"]
    assert sent[0]["role"] == "system"
    assert "<thinking>" in sent[0]["content"]


async def test_astream_reasoning_native_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_target(monkeypatch, "claude-sonnet-4")

    async def _fake_thinking(messages: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        yield StreamDelta("thinking", "native reasoning")
        yield StreamDelta("text", "answer")

    monkeypatch.setattr(LLMGateway, "astream_byom_thinking", staticmethod(_fake_thinking))
    # Must NOT touch the flat/simulated path.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("astream_byom must not run on a native model")
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom))

    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning([{"role": "user", "content": "hi"}], tier="medium")
    )
    assert think == "native reasoning"
    assert answer == "answer"
    assert sources == {"native"}


def test_inject_reasoning_scaffold_does_not_mutate_caller() -> None:
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "U"},
    ]
    out = LLMGateway._inject_reasoning_scaffold(messages, want_json=True)
    # Caller's list + dicts are untouched.
    assert messages[0]["content"] == "SYS"
    # Scaffold appended to the copy's system message, JSON clause included.
    assert out[0]["content"].startswith("SYS")
    assert "<thinking>" in out[0]["content"]
    assert "single JSON object" in out[0]["content"]
    # No code is invited into the reasoning.
    assert "Do NOT put code" in out[0]["content"]


def test_inject_reasoning_scaffold_prepends_when_no_system() -> None:
    messages = [{"role": "user", "content": "U"}]
    out = LLMGateway._inject_reasoning_scaffold(messages, want_json=False)
    assert out[0]["role"] == "system"
    assert "<thinking>" in out[0]["content"]
    assert len(messages) == 1  # original untouched


async def test_acomplete_with_thinking_simulated_json_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_target(monkeypatch, "mistral-small")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "astream_byom",
        staticmethod(_fake_flat_stream(
            ["<thinking>", "reasoning here", "</thinking>", "```json\n", '{"k": "v"}', "\n```"],
            captured,
        )),
    )
    sink_calls: List[Tuple[str, str]] = []

    async def _sink(text: str, source: str) -> None:
        sink_calls.append((text, source))

    answer = await LLMGateway.acomplete_with_thinking(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        on_thinking=_sink,
        enable_thinking=True,
        response_format={"type": "json_object"},
    )
    # Answer is the stripped + fence-sanitized JSON — no <thinking>, no fences.
    assert answer == '{"k": "v"}'
    assert "<thinking>" not in answer
    # Reasoning reached the sink, tagged simulated.
    assert "".join(t for t, _ in sink_calls) == "reasoning here"
    assert {s for _, s in sink_calls} == {"simulated"}
    # Answer got its own token headroom (not starved by the reasoning block).
    assert captured["kwargs"]["max_tokens"] > 4096


async def test_acomplete_with_thinking_no_sink_uses_plain_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No sink → no reasoning engine, no scaffold: byte-identical to ainvoke.
    class _Msg:
        content = "plain answer"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def _fake_ainvoke(**kwargs: Any) -> Any:
        assert "<thinking>" not in kwargs["messages"][0].get("content", "")
        return _Resp()

    monkeypatch.setattr(LLMGateway, "ainvoke", staticmethod(_fake_ainvoke))
    out = await LLMGateway.acomplete_with_thinking(
        [{"role": "system", "content": "S"}],
        on_thinking=None,
        enable_thinking=True,
    )
    assert out == "plain answer"
