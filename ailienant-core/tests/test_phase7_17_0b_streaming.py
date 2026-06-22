# tests/test_phase7_17_0b_streaming.py
"""Streaming native thinking from the coding pipeline (ADR-739).

The planner/coder nodes stop freezing during inference: while they generate the
structured JSON answer they stream the model's native reasoning to the Thought
Box, reusing the Phase 9 thinking stack. Coverage:

  Gateway (``acomplete_with_thinking``):
    G1 thinking branch — reasoning → sink, answer buffered and returned, no ainvoke.
    G2 fallback (thinking off) — delegates to ainvoke WITH response_format, no sink.
    G3 fallback (non-reasoning model) — same delegation, capability-gated.
    G4 socket isolation — a sink failure never aborts generation; sink latches off.
    G5 abort — a CancelledError from the sink propagates (real abort honoured).
    G6 fence strip — a ```json-wrapped answer returns bare, parseable JSON.

  task_service (``_ThinkingStreamer``):
    TS1 — deltas coalesce to broadcast_thinking_chunk; flush drains; the
          NarrationGate channel (broadcast_pipeline_step) is never touched.

  Nodes:
    N1 coder — config seam forwarded to the gateway; edits still parsed.
    N2 coder — thinking off forwards enable_thinking=False; edits still parsed.
    N3 planner — config seam forwarded; the plan still validates.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.runnables import RunnableConfig

from brain.state import MissionSpecification, WBSStep
from tools.llm_gateway import LLMGateway
from tools.stream_delta import StreamDelta

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── helpers ───────────────────────────────────────────────────────────────────


def _target(model: str) -> Any:
    from core.config.byom_config import ModelTarget
    return ModelTarget(
        model=model, provider="anthropic", api_base=None, api_key="sk-test", is_local=False,
    )


def _stream(*deltas: StreamDelta) -> Any:
    """Build a no-arg callable returning an async iterator of the given deltas."""
    def _factory(*_a: Any, **_k: Any) -> AsyncIterator[StreamDelta]:
        async def _gen() -> AsyncIterator[StreamDelta]:
            for d in deltas:
                yield d
        return _gen()
    return _factory


def _ainvoke_returning(content: str) -> AsyncMock:
    return AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
    )


# ── G1 — thinking branch ──────────────────────────────────────────────────────


async def test_g1_thinking_branch_streams_and_buffers() -> None:
    seen: List[str] = []

    async def sink(text: str) -> None:
        seen.append(text)

    ainvoke = _ainvoke_returning("UNUSED")
    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "Plan: "),
             StreamDelta("thinking", "edit foo. "),
             StreamDelta("text", '{"edits": '),
             StreamDelta("text", "[]}"),
         )), \
         patch.object(LLMGateway, "ainvoke", new=ainvoke):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=sink,
            enable_thinking=True,
        )

    assert seen == ["Plan: ", "edit foo. "]      # reasoning reached the sink, in order
    assert out == '{"edits": []}'                # answer buffered + returned
    ainvoke.assert_not_called()                  # streaming branch never calls ainvoke


# ── G2 — fallback when thinking is disabled ───────────────────────────────────


async def test_g2_fallback_uses_ainvoke_with_response_format() -> None:
    sink = AsyncMock()
    ainvoke = _ainvoke_returning('{"edits": []}')
    with patch.object(LLMGateway, "ainvoke", new=ainvoke):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=sink,
            enable_thinking=False,        # disabled → fallback
        )

    assert out == '{"edits": []}'
    sink.assert_not_awaited()
    assert ainvoke.await_count == 1
    assert ainvoke.call_args.kwargs["response_format"] == {"type": "json_object"}


# ── G3 — fallback when the model is not a reasoning model ──────────────────────


async def test_g3_fallback_when_model_not_reasoning() -> None:
    sink = AsyncMock()
    ainvoke = _ainvoke_returning('{"edits": []}')
    with patch("core.config.model_resolver.get_chat_target", return_value=_target("ollama/qwen2.5-coder:7b")), \
         patch.object(LLMGateway, "ainvoke", new=ainvoke):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=sink,
            enable_thinking=True,         # on, but model can't reason → fallback
        )

    assert out == '{"edits": []}'
    sink.assert_not_awaited()
    assert ainvoke.await_count == 1


# ── G4 — a dead sink never aborts generation; it latches off ───────────────────


async def test_g4_socket_failure_does_not_break_generation() -> None:
    calls = {"n": 0}

    async def flaky_sink(_text: str) -> None:
        calls["n"] += 1
        raise ConnectionError("socket closed")

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "a"),
             StreamDelta("thinking", "b"),
             StreamDelta("text", "X"),
             StreamDelta("text", "Y"),
         )):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            on_thinking=flaky_sink,
            enable_thinking=True,
        )

    assert out == "XY"          # the answer is intact despite the dead socket
    assert calls["n"] == 1      # sink latched off after the first failure (no retry storm)


# ── G5 — a real abort propagates ──────────────────────────────────────────────


async def test_g5_cancelled_error_propagates() -> None:
    async def cancelling_sink(_text: str) -> None:
        raise asyncio.CancelledError()

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "a"),
             StreamDelta("text", "X"),
         )):
        with pytest.raises(asyncio.CancelledError):
            await LLMGateway.acomplete_with_thinking(
                [{"role": "user", "content": "x"}],
                model="ailienant/big",
                on_thinking=cancelling_sink,
                enable_thinking=True,
            )


# ── G6 — markdown fences stripped before returning JSON ────────────────────────


async def test_g6_strips_markdown_fences_from_buffered_answer() -> None:
    async def sink(_text: str) -> None:
        return None

    with patch("core.config.model_resolver.get_chat_target", return_value=_target("claude-3-7-sonnet")), \
         patch.object(LLMGateway, "astream_byom_thinking", new=_stream(
             StreamDelta("thinking", "reasoning"),
             StreamDelta("text", "```json\n"),
             StreamDelta("text", '{"edits": []}'),
             StreamDelta("text", "\n```"),
         )):
        out = await LLMGateway.acomplete_with_thinking(
            [{"role": "user", "content": "x"}],
            model="ailienant/big",
            response_format={"type": "json_object"},
            on_thinking=sink,
            enable_thinking=True,
        )

    assert out == '{"edits": []}'
    assert json.loads(out) == {"edits": []}     # downstream parser never sees the fence


# ── TS1 — the task_service thinking coalescer ─────────────────────────────────


async def test_ts1_thinking_streamer_coalesces_and_isolates_narration() -> None:
    from core import task_service as ts_mod

    chunks: List[str] = []
    bcast_thinking = AsyncMock(side_effect=lambda sid, chunk, n=0: chunks.append(chunk))
    bcast_step = AsyncMock()
    with patch.object(ts_mod.vfs_manager, "broadcast_thinking_chunk", bcast_thinking), \
         patch.object(ts_mod.vfs_manager, "broadcast_pipeline_step", bcast_step):
        streamer = ts_mod._ThinkingStreamer("sess-X")
        # Sub-window feeds buffer without an immediate flush…
        await streamer.feed("hel")
        await streamer.feed("lo")
        # …then flush drains the tail in one frame.
        await streamer.flush()

    assert "".join(chunks) == "hello"
    bcast_step.assert_not_called()      # thinking never touches the NarrationGate channel


# ── coder / planner node helpers ──────────────────────────────────────────────


def _make_step(target_file: str = "calc.py", action: str = "edit_file") -> WBSStep:
    return WBSStep(
        step_number=1, target_role="core_dev", action=action,  # type: ignore[arg-type]
        target_file=target_file, description="Bump increment.", status="pending",  # type: ignore[arg-type]
    )


def _make_mission() -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.", scope=["calc.py"], constraints=["No external deps."],
        decisions=["Use the test runner."], tasks=[_make_step()], checks=["Pytest exits 0."],
    )


def _coder_state() -> Dict[str, Any]:
    return {
        "task_id": "coder-test", "mission_spec": _make_mission(), "current_step_id": 1,
        "retry_count": 0, "errors": [], "security_flags": [], "validation_feedback": None,
    }


# ── N1 / N2 — coder forwards the config seam, edits still parse ───────────────


async def _run_coder_with(config: RunnableConfig) -> tuple[Dict[str, Any], AsyncMock]:
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    edit_blob = (
        "### EDIT calc.py\n"
        "<<<<<<< SEARCH\n"
        "    return x + 1\n"
        "=======\n"
        "    return x + 2\n"
        ">>>>>>> REPLACE\n"
    )
    gw = AsyncMock(return_value=edit_blob)
    with patch("core.vfs_middleware.VFSMiddleware.read_safe",
               return_value=VFSReadResult(content="def calculate(x):\n    return x + 1\n")), \
         patch("core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
               new=AsyncMock(return_value=[])), \
         patch("api.websocket_manager.vfs_manager.emit_graph_mutation", new=AsyncMock(return_value=None)), \
         patch("core.response_cache.response_cache.probe", return_value=None), \
         patch("core.response_cache.response_cache.store", new=MagicMock()), \
         patch("tools.llm_gateway.LLMGateway.acomplete_with_thinking", new=gw):
        result = await run_coder_node(_coder_state(), config)
    return result, gw


async def test_n1_coder_forwards_seam_and_parses_edits() -> None:
    async def sink(_t: str) -> None:
        return None

    config: RunnableConfig = {"configurable": {
        "stream_thinking": sink, "enable_native_thinking": True, "thinking_budget_tokens": 2048,
    }}
    result, gw = await _run_coder_with(config)

    kw = gw.call_args.kwargs
    assert kw["on_thinking"] is sink
    assert kw["enable_thinking"] is True
    assert kw["thinking_budget_tokens"] == 2048
    assert "calc.py" in result["pending_patches"]


async def test_n2_coder_thinking_off_still_parses() -> None:
    config: RunnableConfig = {"configurable": {"enable_native_thinking": False}}
    result, gw = await _run_coder_with(config)

    kw = gw.call_args.kwargs
    assert kw["enable_thinking"] is False
    assert kw["on_thinking"] is None
    assert "calc.py" in result["pending_patches"]


# ── N3 — planner forwards the seam, the plan still validates ───────────────────


async def test_n3_planner_forwards_seam_and_validates() -> None:
    async def sink(_t: str) -> None:
        return None

    decision = MagicMock()
    decision.cancelled = False
    decision.effective_model = "ailienant/big"
    decision.holds_lock = False

    gw = AsyncMock(return_value=MissionSpecification(
        outcome="Out.", scope=["s.py"], constraints=["c"], decisions=["d"],
        tasks=[WBSStep(step_number=1, target_role="architect_refactor",  # type: ignore[arg-type]
                       action="read_file", target_file="s.py", description="t.")],
        checks=["ok"],
    ).model_dump_json())

    state: Dict[str, Any] = {
        "task_id": "planner-test", "user_input": "Add a feature.", "workspace_root": "/ws",
        "project_id": "abc", "context_metrics": None, "mission_spec": None, "immutable_wbs": None,
        "errors": [], "retry_count": 0, "current_cost_usd": 0.0, "max_budget_usd": 10.0,
        "vfs_buffer": {}, "terminal_output": "", "parallel_tasks": [], "tci": 45.0, "css": 78.5,
        "provider": "LOCAL", "current_step_id": None, "dirty_buffers": [], "ide_context": "",
        "researcher_skeleton": None,
    }
    config: RunnableConfig = {"configurable": {
        "stream_thinking": sink, "enable_native_thinking": True, "thinking_budget_tokens": 1024,
    }}

    # Planner is a pure WBS engine (no retrieval/cascade); it consumes context_metrics
    # from state. This test pins the thinking-seam forwarding on the WBS draft call.
    with patch("agents.planner.DEBUG_MODE", False), \
         patch("agents.planner.TrajectoryMemoryManager") as traj_cls, \
         patch("tools.llm_gateway.LLMGateway.acomplete_with_thinking", new=gw), \
         patch("agents.planner.ResourceBroker.acquire_or_resolve", new=AsyncMock(return_value=decision)), \
         patch("agents.planner.ResourceBroker.release", new=AsyncMock(return_value=None)):
        traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node
        result = await run_planner_node(state, config)

    assert result.get("mission_spec") is not None
    kw = gw.call_args.kwargs
    assert kw["on_thinking"] is sink
    assert kw["enable_thinking"] is True
    assert kw["thinking_budget_tokens"] == 1024
