# tests/test_phase7_17_checkpoint_gate.py
"""Phase 7.17 — Backend Checkpoint Gate (ADR-739).

Single certification that the 7.17.0-B backend contract holds against the
shipped entry points. Does NOT re-run the detailed rows in
``test_phase7_17_0b_streaming.py``; each row here pins one architectural
invariant that a future refactor could accidentally remove.

Gate rows certified:

  GATEWAY1   fallback delegates ainvoke WITH response_format (no regression).
  GATEWAY2   streaming branch fires the sink; ainvoke is never called.
  ISOLATE1   a dead sink (ConnectionError) never aborts generation.
  FENCE1     thinking bytes go via broadcast_thinking_chunk ONLY — the
             NarrationGate channel (broadcast_pipeline_step) is untouched.
  INJECT1    task_service source injects all three stream_thinking keys on
             the run config (wiring certified by source inspection).
  NODE1      coder node reads stream_thinking from config and forwards it
             to acomplete_with_thinking; edits still parse correctly.

All async cases run under anyio (asyncio backend).
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any, AsyncIterator, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.llm_gateway import LLMGateway
from tools.stream_delta import StreamDelta

pytestmark = pytest.mark.anyio

_PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── helpers ───────────────────────────────────────────────────────────────────


def _target(model: str) -> Any:
    from core.config.byom_config import ModelTarget
    return ModelTarget(
        model=model, provider="anthropic",
        api_base=None, api_key="sk-test", is_local=False,
    )


def _stream(*deltas: StreamDelta) -> Any:
    def factory(*_a: Any, **_k: Any) -> AsyncIterator[StreamDelta]:
        async def gen() -> AsyncIterator[StreamDelta]:
            for d in deltas:
                yield d
        return gen()
    return factory


def _ainvoke_resp(content: str) -> AsyncMock:
    return AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
    )


# ── GATEWAY1 ─────────────────────────────────────────────────────────────────


async def test_gateway1_fallback_delegates_ainvoke_with_response_format() -> None:
    """Thinking disabled → uses ainvoke WITH response_format; streaming branch never fires."""
    ainvoke = _ainvoke_resp('{"edits": []}')
    with patch.object(LLMGateway, "ainvoke", new=ainvoke):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=AsyncMock(),
            enable_thinking=False,
        )
    assert out == '{"edits": []}'
    assert ainvoke.await_count == 1
    # response_format MUST be forwarded — this is the regression guard that keeps
    # structured output working on non-reasoning turns.
    assert ainvoke.call_args.kwargs.get("response_format") == {"type": "json_object"}


# ── GATEWAY2 ─────────────────────────────────────────────────────────────────


async def test_gateway2_streaming_branch_fires_sink_and_ainvoke_not_called() -> None:
    """Thinking on, reasoning model → sink receives deltas; answer buffered; ainvoke never called."""
    seen: List[str] = []

    async def sink(text: str) -> None:
        seen.append(text)

    ainvoke = _ainvoke_resp("UNUSED")
    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "reasoning…"),
             StreamDelta("text", '{"edits": []}'),
         )), \
         patch.object(LLMGateway, "ainvoke", new=ainvoke):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            on_thinking=sink,
            enable_thinking=True,
        )
    assert seen == ["reasoning…"], "sink must receive the thinking delta"
    assert out == '{"edits": []}', "answer must be the buffered text"
    ainvoke.assert_not_called()


# ── ISOLATE1 ─────────────────────────────────────────────────────────────────


async def test_isolate1_dead_sink_never_aborts_generation() -> None:
    """A closed WebSocket (ConnectionError from the sink) must NOT stop code generation."""
    async def dead_sink(_text: str) -> None:
        raise ConnectionError("socket closed")

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "a"),
             StreamDelta("thinking", "b"),
             StreamDelta("text", "result"),
         )):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            on_thinking=dead_sink,
            enable_thinking=True,
        )
    # Generation must complete with the full answer regardless of the dead sink.
    assert out == "result"


# ── FENCE1 ───────────────────────────────────────────────────────────────────


async def test_fence1_thinking_uses_thinking_channel_not_narration_gate() -> None:
    """_ThinkingStreamer must call broadcast_thinking_chunk only; broadcast_pipeline_step
    (the NarrationGate channel) must never be called — thinking must not eat into the
    narration budget."""
    from core import task_service as ts_mod

    chunks: List[str] = []
    bcast_thinking = AsyncMock(side_effect=lambda *a, **_k: chunks.append(a[1]))
    bcast_step = AsyncMock()

    with patch.object(ts_mod.vfs_manager, "broadcast_thinking_chunk", bcast_thinking), \
         patch.object(ts_mod.vfs_manager, "broadcast_pipeline_step", bcast_step):
        streamer = ts_mod._ThinkingStreamer("sess-gate")
        await streamer.feed("hello ")
        await streamer.feed("world")
        await streamer.flush()

    assert "".join(chunks) == "hello world"
    bcast_step.assert_not_called()


# ── INJECT1 ──────────────────────────────────────────────────────────────────


def test_inject1_task_service_injects_all_stream_thinking_keys() -> None:
    """task_service must inject stream_thinking, enable_native_thinking, and
    thinking_budget_tokens on the run config so the nodes can read them."""
    src = (_PKG_ROOT / "core" / "task_service.py").read_text(encoding="utf-8")
    assert '"stream_thinking"' in src, \
        "stream_thinking key missing from task_service configurable injection"
    assert '"enable_native_thinking"' in src, \
        "enable_native_thinking key missing from task_service configurable injection"
    assert '"thinking_budget_tokens"' in src, \
        "thinking_budget_tokens key missing from task_service configurable injection"


# ── NODE1 ─────────────────────────────────────────────────────────────────────


async def test_node1_coder_forwards_stream_thinking_to_gateway_and_parses_edits() -> None:
    """run_coder_node must read stream_thinking from config.configurable and pass
    it as on_thinking to acomplete_with_thinking; the edited diff must still parse."""
    from core.vfs_middleware import VFSReadResult
    from langchain_core.runnables import RunnableConfig
    from brain.state import MissionSpecification, WBSStep
    from agents.coder import run_coder_node

    async def sink(_t: str) -> None:
        return None

    step = WBSStep(
        step_number=1, target_role="core_dev",   # type: ignore[arg-type]
        action="edit_file", target_file="f.py",  # type: ignore[arg-type]
        description="bump", status="pending",    # type: ignore[arg-type]
    )
    mission = MissionSpecification(
        outcome="T", scope=["f.py"], constraints=["c"],
        decisions=["d"], tasks=[step], checks=["ok"],
    )
    state = {
        "task_id": "gate1", "mission_spec": mission, "current_step_id": 1,
        "retry_count": 0, "errors": [], "security_flags": [], "validation_feedback": None,
    }
    config: RunnableConfig = {"configurable": {
        "stream_thinking": sink,
        "enable_native_thinking": True,
        "thinking_budget_tokens": 2048,
    }}
    edit_json = (
        '{"edits": [{"file_path": "f.py", '
        '"search_block": "    return 1", "replace_block": "    return 2"}]}'
    )
    gw = AsyncMock(return_value=edit_json)
    with patch("core.vfs_middleware.VFSMiddleware.read_safe",
               return_value=VFSReadResult(content="def f():\n    return 1\n")), \
         patch("core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
               new=AsyncMock(return_value=[])), \
         patch("api.websocket_manager.vfs_manager.emit_graph_mutation",
               new=AsyncMock(return_value=None)), \
         patch("core.response_cache.response_cache.probe", return_value=None), \
         patch("core.response_cache.response_cache.store", new=MagicMock()), \
         patch("tools.llm_gateway.LLMGateway.acomplete_with_thinking", new=gw):
        result = await run_coder_node(state, config)

    kw = gw.call_args.kwargs
    assert kw.get("on_thinking") is sink, \
        "coder must forward stream_thinking as on_thinking to the gateway"
    assert kw.get("enable_thinking") is True
    assert "f.py" in result.get("pending_patches", {}), \
        "edits must still parse correctly regardless of the thinking seam"
