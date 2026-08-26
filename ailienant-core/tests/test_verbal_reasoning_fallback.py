"""Tests for the shared reasoning engine's safety-by-default contract.

``LLMGateway.astream_reasoning`` decides native vs simulated reasoning, and
additionally decides whether scaffolding is even safe to attempt. Regression
coverage for the 2026-07-25 incident (a recurrence of DEBT-013's failure
class): a non-reasoning model asked to narrate free-form reasoning *and*
produce strict, machine-parsed output in the same completion silently
corrupts that output. Fixed by making scaffolding OFF by default — a caller
must explicitly pass ``free_form_answer=True`` to opt in, and a
``response_format`` request always overrides that opt-in.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List, Tuple
from unittest.mock import AsyncMock

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


def _fake_ainvoke_returning(content: str, captured: dict[str, Any] | None = None):
    class _Msg:
        pass

    class _Choice:
        pass

    class _Resp:
        pass

    async def _fake(*args: Any, **kwargs: Any) -> Any:
        if captured is not None:
            captured.update(kwargs)
            if args:
                captured["messages"] = args[0]
        msg = _Msg()
        msg.content = content  # type: ignore[attr-defined]
        choice = _Choice()
        choice.message = msg  # type: ignore[attr-defined]
        resp = _Resp()
        resp.choices = [choice]  # type: ignore[attr-defined]
        return resp
    return _fake


# ── Safety default: no scaffold unless explicitly requested ────────────────


async def test_astream_reasoning_default_is_unscaffolded_on_non_native_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Coder-shape regression test: no response_format, no free_form_answer
    (the default) — the model must never see a scaffold, and no thinking delta
    is ever produced. The absence of the scaffold call IS the fix."""
    _patch_target(monkeypatch, "llama3.1")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "astream_byom",
        staticmethod(_fake_flat_stream(["plain answer, no tags"], captured)),
    )
    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning([{"role": "user", "content": "hi"}], tier="medium")
    )
    assert think == ""
    assert answer == "plain answer, no tags"
    assert sources == set()
    # The original, unscaffolded message list reached the model verbatim.
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


async def test_astream_reasoning_scaffolds_only_when_free_form_answer_true(
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
        LLMGateway.astream_reasoning(
            [{"role": "user", "content": "hi"}], tier="medium", free_form_answer=True,
        )
    )
    assert think == "I weigh the trade-offs"
    assert answer == "the answer"
    assert sources == {"simulated"}
    sent = captured["messages"]
    assert sent[0]["role"] == "system"
    assert "<thinking>" in sent[0]["content"]


async def test_astream_reasoning_response_format_never_scaffolds_even_with_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """response_format always overrides free_form_answer — a caller contradicting
    itself (opting in to scaffolding on a strict-output call) is not honored."""
    _patch_target(monkeypatch, "llama3.1")

    def _boom_stream(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("astream_byom (flat/scaffolded path) must not run")
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom_stream))

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "ainvoke", staticmethod(_fake_ainvoke_returning('{"ok": true}', captured)),
    )
    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning(
            [{"role": "user", "content": "hi"}], tier="medium",
            response_format={"type": "json_object"}, free_form_answer=True,
        )
    )
    assert think == ""
    assert answer == '{"ok": true}'
    assert captured["response_format"] == {"type": "json_object"}


async def test_astream_reasoning_response_format_non_native_routes_through_ainvoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Planner-shape regression test: response_format on a non-native model
    restores true pre-11.5 behavior (ainvoke, provider-level JSON enforcement),
    not a scaffolded/simulated stream."""
    _patch_target(monkeypatch, "mistral-small")

    def _boom_stream(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("astream_byom must not run when response_format is set")
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom_stream))

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "ainvoke",
        staticmethod(_fake_ainvoke_returning('{"tasks": []}', captured)),
    )
    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
            tier="medium", response_format={"type": "json_object"},
        )
    )
    assert think == ""
    assert answer == '{"tasks": []}'
    assert sources == set()
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["model"] == "ailienant/medium"
    # The original, unscaffolded messages reached ainvoke verbatim.
    assert captured["messages"][0]["content"] == "S"


async def test_astream_reasoning_native_with_response_format_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard on DEBT-013's existing fix: a native model with
    response_format set must be completely unaffected by this hardening."""
    _patch_target(monkeypatch, "claude-sonnet-4")
    captured: dict[str, Any] = {}

    async def _fake_thinking(messages: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        captured.update(kwargs)
        yield StreamDelta("thinking", "native reasoning")
        yield StreamDelta("text", '{"ok": 1}')

    monkeypatch.setattr(LLMGateway, "astream_byom_thinking", staticmethod(_fake_thinking))

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("native path must not fall back to ainvoke or astream_byom")
    monkeypatch.setattr(LLMGateway, "ainvoke", staticmethod(_boom))
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom))

    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning(
            [{"role": "user", "content": "hi"}], tier="medium",
            response_format={"type": "json_object"},
        )
    )
    assert think == "native reasoning"
    assert answer == '{"ok": 1}'
    assert sources == {"native"}


async def test_astream_reasoning_native_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_target(monkeypatch, "claude-sonnet-4")

    async def _fake_thinking(messages: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        yield StreamDelta("thinking", "native reasoning")
        yield StreamDelta("text", "answer")

    monkeypatch.setattr(LLMGateway, "astream_byom_thinking", staticmethod(_fake_thinking))

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("astream_byom must not run on a native model")
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom))

    think, answer, sources = await _collect(
        LLMGateway.astream_reasoning([{"role": "user", "content": "hi"}], tier="medium")
    )
    assert think == "native reasoning"
    assert answer == "answer"
    assert sources == {"native"}


# ── Scaffold injector ───────────────────────────────────────────────────────


def test_inject_reasoning_scaffold_does_not_mutate_caller() -> None:
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "U"},
    ]
    out = LLMGateway._inject_reasoning_scaffold(messages)
    assert messages[0]["content"] == "SYS"  # caller's list + dicts untouched
    assert out[0]["content"].startswith("SYS")
    assert "<thinking>" in out[0]["content"]
    assert "Do NOT put code" in out[0]["content"]


def test_inject_reasoning_scaffold_prepends_when_no_system() -> None:
    messages = [{"role": "user", "content": "U"}]
    out = LLMGateway._inject_reasoning_scaffold(messages)
    assert out[0]["role"] == "system"
    assert "<thinking>" in out[0]["content"]
    assert len(messages) == 1  # original untouched


# ── acomplete_with_thinking end-to-end ──────────────────────────────────────


async def test_acomplete_with_thinking_json_mode_on_non_native_never_scaffolds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Planner regression test end-to-end: on_thinking wired + enable_thinking
    + response_format on a non-native model → clean ainvoke-backed answer, sink
    never invoked, no scaffold ever sent."""
    _patch_target(monkeypatch, "mistral-small")

    def _boom_stream(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("astream_byom must not run")
    monkeypatch.setattr(LLMGateway, "astream_byom", staticmethod(_boom_stream))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        LLMGateway, "ainvoke", staticmethod(_fake_ainvoke_returning('{"k": "v"}', captured)),
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
    assert answer == '{"k": "v"}'
    assert sink_calls == []  # no reasoning ever attempted
    assert captured["messages"][0]["content"] == "S"  # never scaffolded


async def test_acomplete_with_thinking_no_sink_uses_plain_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No sink → no reasoning engine, no scaffold: byte-identical to ainvoke.
    async def _fake_ainvoke(**kwargs: Any) -> Any:
        assert "<thinking>" not in kwargs["messages"][0].get("content", "")
        return await _fake_ainvoke_returning("plain answer")(**kwargs)

    monkeypatch.setattr(LLMGateway, "ainvoke", staticmethod(_fake_ainvoke))
    out = await LLMGateway.acomplete_with_thinking(
        [{"role": "system", "content": "S"}],
        on_thinking=None,
        enable_thinking=True,
    )
    assert out == "plain answer"


# ── Free-form callers stay opted in (catches an accidental future removal) ──


async def test_analyst_stream_opts_into_free_form_scaffolding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import analyst as analyst_mod

    captured: dict[str, Any] = {}

    async def _fake_astream_reasoning(messages: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        captured.update(kwargs)
        yield StreamDelta("text", "hi there")

    async def _noop_reasoning(_t: str, _s: str) -> None:
        return None

    # analyst.py imports LLMGateway locally (deferred), so patch the class itself.
    monkeypatch.setattr(LLMGateway, "astream_reasoning", staticmethod(_fake_astream_reasoning))
    parts: List[str] = []
    async for chunk in analyst_mod.generate_analyst_reply_stream(
        "hello", session_id="s1", on_reasoning=_noop_reasoning, enable_thinking=True,
    ):
        parts.append(chunk)
    assert captured.get("free_form_answer") is True


async def test_live_chat_stream_opts_into_free_form_scaffolding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import task_service as ts_mod

    captured: dict[str, Any] = {}

    async def _fake_astream_reasoning(messages: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        captured.update(kwargs)
        yield StreamDelta("text", "hi there")

    monkeypatch.setattr(
        LLMGateway, "astream_reasoning", staticmethod(_fake_astream_reasoning),
    )
    monkeypatch.setattr(ts_mod.vfs_manager, "broadcast_thinking_chunk", AsyncMock())
    monkeypatch.setattr(ts_mod.vfs_manager, "broadcast_token", AsyncMock())

    ts = ts_mod.TaskService()
    reply_parts: List[str] = []
    await ts._stream_with_thinking("s1", [{"role": "user", "content": "hi"}], reply_parts, 4096)
    assert captured.get("free_form_answer") is True
