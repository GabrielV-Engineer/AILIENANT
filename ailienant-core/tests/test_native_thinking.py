# tests/test_native_thinking.py
"""Phase 9 DoD — Native Thinking (ADR-707).

Covers the backend half of the real-time reasoning stream:

  1. Capability gate — ``_supports_native_thinking`` recognises reasoning models
     and rejects plain ones.
  2. Gateway bifurcation — ``astream_byom_thinking`` yields ``StreamDelta``
     values tagged "thinking" then "text", in order, for a capable model.
  3. ``thinking`` kwarg is appended (with the budget) ONLY for a capable model,
     and omitted entirely for an incapable one (zero-regression fallback).
  4. Fallback — a model that never emits ``reasoning_content`` produces zero
     "thinking" deltas; the answer stream is intact.
  5. Ledger — usage on the final chunk is still recorded (FinOps integrity,
     completion path).
  6. Orchestration demux — ``_stream_chat_answer`` routes reasoning →
     ``broadcast_thinking_chunk`` and answer → ``broadcast_token``, thinking
     first; reasoning is NEVER persisted to history (cognitive isolation).
  7. ``TaskPayload`` defaults — thinking ON, budget 4096 — and wire round-trip.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskPayload, TaskService
from core.token_ledger import token_ledger
from tools.llm_gateway import LLMGateway, _supports_native_thinking
from tools.stream_delta import StreamDelta

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ──────────────────────────────────────────────────────────────────────────────
# litellm streaming stubs (mirror tests/test_abort_mesh.py shapes, + reasoning)
# ──────────────────────────────────────────────────────────────────────────────


class _StubDelta:
    def __init__(self, content: Optional[str] = None, reasoning_content: Optional[str] = None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content


class _StubChoice:
    def __init__(self, delta: _StubDelta) -> None:
        self.delta = delta


class _StubUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _StubChunk:
    def __init__(self, delta: Optional[_StubDelta] = None, usage: Optional[_StubUsage] = None) -> None:
        self.choices = [_StubChoice(delta)] if delta is not None else []
        self.usage = usage


def _thinking_then_text_stream(**_kwargs: Any) -> Any:
    """A reasoning model: two thinking deltas, two answer deltas, then usage."""
    async def _gen() -> AsyncIterator[_StubChunk]:
        yield _StubChunk(_StubDelta(reasoning_content="Let me "))
        yield _StubChunk(_StubDelta(reasoning_content="think… "))
        yield _StubChunk(_StubDelta(content="Hello "))
        yield _StubChunk(_StubDelta(content="world"))
        yield _StubChunk(usage=_StubUsage(prompt=10, completion=20))
    return _gen()


def _text_only_stream(**_kwargs: Any) -> Any:
    """A plain model: no reasoning_content ever."""
    async def _gen() -> AsyncIterator[_StubChunk]:
        yield _StubChunk(_StubDelta(content="just "))
        yield _StubChunk(_StubDelta(content="text"))
        yield _StubChunk(usage=_StubUsage(prompt=5, completion=4))
    return _gen()


def _anthropic_target() -> Any:
    from core.config.byom_config import ModelTarget
    return ModelTarget(
        model="anthropic/claude-3-7-sonnet", provider="anthropic",
        api_base=None, api_key="sk-test", is_local=False,
    )


def _local_plain_target() -> Any:
    from core.config.byom_config import ModelTarget
    return ModelTarget(
        model="ollama/qwen2.5-coder:7b", provider="ollama",
        api_base="http://127.0.0.1:11434", api_key="", is_local=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Capability gate
# ──────────────────────────────────────────────────────────────────────────────


def test_supports_native_thinking_gate() -> None:
    assert _supports_native_thinking("anthropic/claude-3-7-sonnet") is True
    assert _supports_native_thinking("deepseek-r1:32b") is True
    assert _supports_native_thinking("ollama/qwq:32b") is True
    assert _supports_native_thinking("ollama/qwen2.5-coder:7b") is False
    assert _supports_native_thinking("gpt-4o") is False
    assert _supports_native_thinking("") is False


# ──────────────────────────────────────────────────────────────────────────────
# 1b. supports_native_thinking (13.1.3) — the runtime-probed check every call
# site now actually uses. `_supports_native_thinking` above is retained only
# as its offline fallback. Regression coverage for N2: a model absent from the
# hint list but reporting real thinking capability at the runtime (`gemma4`,
# live-verified: capabilities=["completion","tools","thinking"]) must now be
# recognised — before this fix it silently ran the free-form narration pass
# in place of its own real reasoning, on every single call, forever.
# ──────────────────────────────────────────────────────────────────────────────


async def test_supports_native_thinking_trusts_a_positive_probe_even_off_the_hint_list() -> None:
    from core.config.model_resolver import RuntimeCapabilities
    from tools.llm_gateway import supports_native_thinking

    target = AsyncMock()
    target.model = "ollama_chat/gemma4:e4b"  # NOT in _NATIVE_THINKING_MODEL_HINTS
    with patch(
        "core.config.model_resolver.probe_runtime_capabilities",
        new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
    ):
        assert await supports_native_thinking(target) is True


async def test_supports_native_thinking_trusts_a_genuine_negative_probe() -> None:
    """A model the runtime successfully queried and which reports NO thinking
    capability must be trusted as a real negative, not retried against the
    offline substring list."""
    from core.config.model_resolver import RuntimeCapabilities
    from tools.llm_gateway import supports_native_thinking

    target = AsyncMock()
    target.model = "ollama_chat/qwq:32b"  # IS on the hint list, but the probe wins
    with patch(
        "core.config.model_resolver.probe_runtime_capabilities",
        new=AsyncMock(return_value=RuntimeCapabilities(context_length=32_768, supports_thinking=False)),
    ):
        assert await supports_native_thinking(target) is False


async def test_supports_native_thinking_falls_back_to_the_hint_list_when_the_probe_is_unknown() -> None:
    """A non-Ollama provider (or an unreachable one) has no equivalent probe —
    the offline substring heuristic is the correct fallback, not a hard False."""
    from core.config.model_resolver import RuntimeCapabilities
    from tools.llm_gateway import supports_native_thinking

    target = AsyncMock()
    target.model = "anthropic/claude-3-7-sonnet"
    with patch(
        "core.config.model_resolver.probe_runtime_capabilities",
        new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
    ):
        assert await supports_native_thinking(target) is True


async def test_supports_native_thinking_none_target_is_false() -> None:
    from tools.llm_gateway import supports_native_thinking

    assert await supports_native_thinking(None) is False


# ──────────────────────────────────────────────────────────────────────────────
# 2. Gateway bifurcation — ordered thinking → text deltas (NT1)
# ──────────────────────────────────────────────────────────────────────────────


async def test_astream_byom_thinking_bifurcates_ordered() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_thinking_then_text_stream)):
        deltas: List[StreamDelta] = []
        async for d in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1",
        ):
            deltas.append(d)

    kinds = [d.kind for d in deltas]
    assert kinds == ["thinking", "thinking", "text", "text"]
    thinking = "".join(d.text for d in deltas if d.kind == "thinking")
    answer = "".join(d.text for d in deltas if d.kind == "text")
    assert thinking == "Let me think… "
    assert answer == "Hello world"


# ──────────────────────────────────────────────────────────────────────────────
# 3. thinking kwarg appended only for capable model + budget propagation (NT5)
# ──────────────────────────────────────────────────────────────────────────────


async def test_thinking_kwarg_capability_and_budget() -> None:
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        captured.clear()
        captured.update(kwargs)
        return _thinking_then_text_stream(**kwargs)

    # Capable model → thinking config present with our budget.
    with patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_capture)):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1",
            enable_thinking=True, thinking_budget_tokens=2048,
        ):
            pass
    assert captured.get("thinking") == {"type": "enabled", "budget_tokens": 2048}

    # Incapable model → no thinking kwarg at all (fallback).
    with patch("core.config.model_resolver.get_chat_target", return_value=_local_plain_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_capture)):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1", enable_thinking=True,
        ):
            pass
    assert "thinking" not in captured

    # enable_thinking=False on a capable model → still no thinking kwarg.
    with patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_capture)):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1", enable_thinking=False,
        ):
            pass
    assert "thinking" not in captured


# ──────────────────────────────────────────────────────────────────────────────
# 3b. thinking kwarg self-heals when the transport rejects it — regression for a
#     model whose capability probe says "can reason" while litellm's provider
#     transport still 400s on the kwarg (observed: Ollama's own /api/show lists
#     `thinking` for gemma4:e4b, but litellm's `ollama_chat` custom provider does
#     not forward an OpenAI/Anthropic-shaped `thinking` param at all).
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _clear_thinking_memo() -> Any:
    from tools.llm_gateway import _THINKING_PARAM_UNSUPPORTED
    _THINKING_PARAM_UNSUPPORTED.clear()
    yield
    _THINKING_PARAM_UNSUPPORTED.clear()


def _thinking_rejecting(**kwargs: Any) -> Any:
    """Stub: 400 (UnsupportedParamsError) while `thinking` is present, succeed once it's stripped."""
    if "thinking" in kwargs:
        from litellm.exceptions import UnsupportedParamsError
        raise UnsupportedParamsError(
            "ollama_chat does not support parameters: ['thinking']",
            llm_provider="ollama_chat", model=kwargs.get("model"),
        )
    return _thinking_then_text_stream(**kwargs)


async def test_thinking_param_self_heals_on_transport_rejection(_clear_thinking_memo: Any) -> None:
    from tools.llm_gateway import _THINKING_PARAM_UNSUPPORTED

    call_kwargs: List[dict[str, Any]] = []

    def _capture_then_reject(**kwargs: Any) -> Any:
        call_kwargs.append(dict(kwargs))
        return _thinking_rejecting(**kwargs)

    # First call: transport rejects `thinking` → gateway strips it, retries once
    # within the same call, and remembers the model.
    with patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_capture_then_reject)):
        deltas: List[StreamDelta] = []
        async for d in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1", enable_thinking=True,
        ):
            deltas.append(d)

    assert len(call_kwargs) == 2                # failed attempt + one retry
    assert "thinking" in call_kwargs[0]          # sent...
    assert "thinking" not in call_kwargs[1]      # ...then stripped
    assert [d.text for d in deltas if d.kind == "text"] == ["Hello ", "world"]
    assert "anthropic/claude-3-7-sonnet" in _THINKING_PARAM_UNSUPPORTED

    # Second, independent call for the same model: never pays the failed round-trip again.
    call_kwargs.clear()
    with patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_capture_then_reject)):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1", enable_thinking=True,
        ):
            pass
    assert len(call_kwargs) == 1                 # no failed attempt this time
    assert "thinking" not in call_kwargs[0]


# ──────────────────────────────────────────────────────────────────────────────
# 4. Fallback — no reasoning_content → zero thinking deltas (NT2)
# ──────────────────────────────────────────────────────────────────────────────


async def test_no_reasoning_yields_no_thinking_deltas() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=_local_plain_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_text_only_stream)):
        deltas: List[StreamDelta] = []
        async for d in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1",
        ):
            deltas.append(d)

    assert all(d.kind == "text" for d in deltas)
    assert "".join(d.text for d in deltas) == "just text"


# ──────────────────────────────────────────────────────────────────────────────
# 5. Ledger still records usage (FinOps integrity, completion path)
# ──────────────────────────────────────────────────────────────────────────────


async def test_thinking_stream_records_usage() -> None:
    token_ledger.reset()
    before = token_ledger.snapshot()
    with patch("core.config.model_resolver.get_chat_target", return_value=_local_plain_target()), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=_thinking_then_text_stream)):
        async for _ in LLMGateway.astream_byom_thinking(
            [{"role": "user", "content": "x"}], session_id="s1",
        ):
            pass
    after = token_ledger.snapshot()
    # ollama → local tier; prompt(10)+completion(20) credited to local_tokens.
    assert after["local_tokens"] - before["local_tokens"] == 30
    token_ledger.reset()


# ──────────────────────────────────────────────────────────────────────────────
# 6. Orchestration demux — thinking → Thought Box, answer → bubble,
#    reasoning never persisted (cognitive isolation)
# ──────────────────────────────────────────────────────────────────────────────


async def test_stream_chat_answer_demuxes_channels() -> None:
    ts = TaskService()

    order: List[str] = []
    broadcast_thinking = AsyncMock(side_effect=lambda *a, **k: order.append("thinking"))
    broadcast_token = AsyncMock(side_effect=lambda *a, **k: order.append("text"))
    broadcast_stream_end = AsyncMock()
    appended: List[tuple[str, str]] = []

    def _thinking_deltas(*_a: Any, **_k: Any) -> AsyncIterator[StreamDelta]:
        async def _gen() -> AsyncIterator[StreamDelta]:
            yield StreamDelta("thinking", "reason A ")
            yield StreamDelta("thinking", "reason B")
            yield StreamDelta("text", "Final ")
            yield StreamDelta("text", "answer")
        return _gen()

    with patch("tools.llm_gateway.LLMGateway.astream_byom_thinking", side_effect=_thinking_deltas), \
         patch("core.config.model_resolver.get_chat_target", return_value=_anthropic_target()), \
         patch.object(TaskService, "_build_rag_context", new=AsyncMock(return_value="")), \
         patch.object(TaskService, "_append_history", side_effect=lambda sid, role, content: appended.append((role, content))), \
         patch("core.task_service.vfs_manager.broadcast_thinking_chunk", broadcast_thinking), \
         patch("core.task_service.vfs_manager.broadcast_token", broadcast_token), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end):
        await ts._stream_chat_answer(
            "sess-T", "explain this", None,
            enable_native_thinking=True, thinking_budget_tokens=4096,
        )

    # Thought Box received the reasoning; chat bubble received the answer.
    assert broadcast_thinking.await_count >= 1
    assert broadcast_token.await_count >= 1
    # Thinking is flushed before the answer renders.
    assert order.index("thinking") < order.index("text")
    # Cognitive isolation: only the answer text is persisted, never reasoning.
    assistant_turns = [c for r, c in appended if r == "assistant"]
    assert assistant_turns == ["Final answer"]
    assert all("reason" not in c for c in assistant_turns)


# ──────────────────────────────────────────────────────────────────────────────
# 7. TaskPayload defaults + wire round-trip
# ──────────────────────────────────────────────────────────────────────────────


def test_task_payload_thinking_defaults() -> None:
    p = TaskPayload(task_prompt="hi", dirty_buffers=[])
    assert p.enable_native_thinking is True
    assert p.thinking_budget_tokens == 4096

    # Explicit opt-out round-trips.
    p2 = TaskPayload(task_prompt="hi", dirty_buffers=[], enable_native_thinking=False, thinking_budget_tokens=1024)
    assert p2.enable_native_thinking is False
    assert p2.thinking_budget_tokens == 1024
