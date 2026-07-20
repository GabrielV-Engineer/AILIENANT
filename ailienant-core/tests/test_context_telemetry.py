"""Context-utilization telemetry — benchmark spike.

Covers: telemetry record shape for both instrumented sources (ContextPipeline
and the summarizer), byte-identical outputs before/after instrumentation,
never-raises-on-telemetry-failure, the session_start_time carry-forward
resolver (both a mocked unit test and a real, non-mocked checkpointer round
trip), the no-double-tokenization guard on the summarizer's shared sink, and
the synthetic long-session corpus generator's determinism + range
characterization.

Test-only; no production code depends on this module.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint

from brain.checkpoint import HybridCheckpointer
from brain.context_pipeline import ContextChunk, ContextPipeline
from brain.state import LLMProfile
from brain.summarizer import (
    KEEP_LAST_N,
    _run_summarize_node_core,
    run_summarize_node,
)
from core.benchmark.session_corpus import generate_corpus
from core.task_service import TaskService
from tools.token_counter import PrecisionTokenCounter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _chunk(brain: str, label: str, n_words: int) -> ContextChunk:
    return ContextChunk(body=" ".join([label] * n_words), brain=brain, label=label).measure()


def _seeded_pipeline(**kwargs: Any) -> ContextPipeline:
    """A pipeline with identical, deterministic content across independent instances."""
    pipe = ContextPipeline(total_token_budget=10_000, **kwargs)
    pipe.foundation.add(_chunk("foundation", "FND", 20))
    pipe.project.add(_chunk("project", "PRJ", 20))
    pipe.memory.add(_chunk("memory", "MEM", 20))
    pipe.conversation.add(_chunk("conversation", "CONV", 30))
    pipe.execution.add(_chunk("execution", "EXE", 30))
    return pipe


def _build_state(num_messages: int, context_window: int = 8192, task_id: str = "test-task") -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message content {i}"}
            for i in range(num_messages)
        ],
        "active_llm_profile": LLMProfile(
            model_name="gpt-4", parameters_b=7.0, context_window=context_window, quantization="q4",
        ),
    }


# ---------------------------------------------------------------------------
# Telemetry record shape
# ---------------------------------------------------------------------------

async def test_pipeline_telemetry_record_shape() -> None:
    with patch("core.telemetry_log.log_context_utilization") as mock_log:
        pipe = _seeded_pipeline(session_id="sess-1", session_start_time=None)
        result = await pipe.assemble()

    assert mock_log.call_count == 1
    kwargs = mock_log.call_args.kwargs
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["source"] == "pipeline"
    assert kwargs["total_tokens"] == result.total_tokens
    assert kwargs["token_budget"] == 10_000
    assert kwargs["l1_tokens"] == result.l1_tokens
    assert kwargs["l2_tokens"] == result.l2_tokens
    assert kwargs["l3_tokens"] == result.l3_tokens
    assert kwargs["l4_tokens"] == result.l4_tokens
    assert kwargs["l5_tokens"] == result.l5_tokens
    assert kwargs["l4_evicted"] == result.l4_evicted
    assert kwargs["l5_truncated"] == result.l5_truncated
    assert kwargs["duration_s"] == 0.0  # no session_start_time supplied


async def test_summarizer_telemetry_record_shape() -> None:
    state = _build_state(20, context_window=10, task_id="sess-2")
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "summary"

    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke, \
         patch("core.telemetry_log.log_context_utilization") as mock_log:
        mock_invoke.return_value = mock_response
        await run_summarize_node(state)

    assert mock_log.call_count == 1
    kwargs = mock_log.call_args.kwargs
    assert kwargs["session_id"] == "sess-2"
    assert kwargs["source"] == "summarizer"
    assert kwargs["total_tokens"] > 0
    assert kwargs["token_budget"] == 10
    assert kwargs["turn_count"] == 20
    # Layer fields are pipeline-only — never fabricated for the summarizer source.
    assert "l1_tokens" not in kwargs


# ---------------------------------------------------------------------------
# Byte-identical outputs
# ---------------------------------------------------------------------------

async def test_pipeline_byte_identical_with_and_without_telemetry_failure() -> None:
    # Two SEPARATE, identically-seeded instances — assemble() mutates layer
    # state in place (FIFO eviction / tail-truncation), so re-using one
    # instance across two calls would not isolate telemetry's effect.
    with patch("core.telemetry_log.log_context_utilization"):
        pipe_a = _seeded_pipeline()
        result_a = await pipe_a.assemble()

    with patch("core.telemetry_log.log_context_utilization", side_effect=RuntimeError("boom")):
        pipe_b = _seeded_pipeline()
        result_b = await pipe_b.assemble()  # must not raise

    assert result_a == result_b


async def test_summarizer_all_five_paths_byte_identical() -> None:
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "summary"

    cases = [
        # (label, state_builder, patcher)
        ("no_op_short", lambda: _build_state(KEEP_LAST_N), None),
        ("no_op_within_budget", lambda: _build_state(100, context_window=200_000), None),
        ("success", lambda: _build_state(20, context_window=10), "success"),
        ("exception_fallback", lambda: _build_state(20, context_window=10), "exception"),
    ]

    for _label, builder, mode in cases:
        state_wrapper = builder()
        state_core = builder()

        if mode == "success":
            with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mi:
                mi.return_value = mock_response
                expected = await _run_summarize_node_core(state_core)
            with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mi2:
                mi2.return_value = mock_response
                actual = await run_summarize_node(state_wrapper)
        elif mode == "exception":
            with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock, side_effect=RuntimeError("x")):
                expected = await _run_summarize_node_core(state_core)
            with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock, side_effect=RuntimeError("x")):
                actual = await run_summarize_node(state_wrapper)
        else:
            expected = await _run_summarize_node_core(state_core)
            actual = await run_summarize_node(state_wrapper)

        assert actual == expected, f"case={_label}"


async def test_summarizer_cancelled_path_byte_identical() -> None:
    from core.resource_manager import BrokerDecision

    cancelled_decision = BrokerDecision(cancelled=True, effective_model="gpt-4", holds_lock=False)

    state_core = _build_state(20, context_window=10)
    with patch("brain.summarizer.ResourceBroker.acquire_or_resolve", new_callable=AsyncMock) as mock_acquire:
        mock_acquire.return_value = cancelled_decision
        expected = await _run_summarize_node_core(state_core)

    state_wrapper = _build_state(20, context_window=10)
    with patch("brain.summarizer.ResourceBroker.acquire_or_resolve", new_callable=AsyncMock) as mock_acquire2:
        mock_acquire2.return_value = cancelled_decision
        actual = await run_summarize_node(state_wrapper)

    assert actual == expected


# ---------------------------------------------------------------------------
# Never-raises-on-telemetry-failure
# ---------------------------------------------------------------------------

async def test_pipeline_telemetry_never_raises() -> None:
    with patch("core.telemetry_log.log_context_utilization", side_effect=RuntimeError("boom")):
        pipe = _seeded_pipeline()
        result = await pipe.assemble()  # must not raise
    assert result.total_tokens > 0


async def test_summarizer_telemetry_never_raises() -> None:
    state = _build_state(KEEP_LAST_N)
    with patch("core.telemetry_log.log_context_utilization", side_effect=RuntimeError("boom")):
        result = await run_summarize_node(state)  # must not raise
    assert result == {}


# ---------------------------------------------------------------------------
# No-op-path coverage + no-double-tokenization
# ---------------------------------------------------------------------------

async def test_summarizer_no_op_short_path_emits_zero_tokens() -> None:
    state = _build_state(KEEP_LAST_N)
    with patch("core.telemetry_log.log_context_utilization") as mock_log:
        result = await run_summarize_node(state)

    assert result == {}
    assert mock_log.call_count == 1
    kwargs = mock_log.call_args.kwargs
    assert kwargs["total_tokens"] == 0
    assert kwargs["token_budget"] == 0
    assert kwargs["turn_count"] == KEEP_LAST_N


async def test_summarizer_no_double_tokenization() -> None:
    state = _build_state(20, context_window=10)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "summary"

    real_estimate = PrecisionTokenCounter.estimate_with_buffer
    call_count = {"n": 0}

    def _counting_estimate(text: str, model: str = "gpt-4") -> int:
        call_count["n"] += 1
        return real_estimate(text, model)

    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke, \
         patch("brain.summarizer.PrecisionTokenCounter.estimate_with_buffer", side_effect=_counting_estimate):
        mock_invoke.return_value = mock_response
        result = await run_summarize_node(state)

    assert "messages" in result
    assert call_count["n"] == 1, "tokenization must happen exactly once per invocation"


# ---------------------------------------------------------------------------
# session_start_time carry-forward — mocked unit test
# ---------------------------------------------------------------------------

def test_resolve_session_start_time_cold_thread_returns_fresh_now() -> None:
    with patch("brain.checkpoint.hybrid_checkpointer.get_tuple", return_value=None):
        before = __import__("time").time()
        value = TaskService._resolve_session_start_time("some-session")
        after = __import__("time").time()
    assert before <= value <= after


def test_resolve_session_start_time_warm_thread_carries_forward() -> None:
    fake_tuple = MagicMock()
    fake_tuple.checkpoint = {"channel_values": {"session_start_time": 12345.6789}}
    with patch("brain.checkpoint.hybrid_checkpointer.get_tuple", return_value=fake_tuple):
        value = TaskService._resolve_session_start_time("some-session")
    assert value == 12345.6789


def test_resolve_session_start_time_checkpointer_fault_falls_back() -> None:
    with patch("brain.checkpoint.hybrid_checkpointer.get_tuple", side_effect=RuntimeError("db down")):
        before = __import__("time").time()
        value = TaskService._resolve_session_start_time("some-session")
        after = __import__("time").time()
    assert before <= value <= after


# ---------------------------------------------------------------------------
# session_start_time carry-forward — real (non-mocked) checkpointer round trip
# ---------------------------------------------------------------------------

def test_session_start_time_real_checkpointer_round_trip(tmp_path: Any) -> None:
    """Exercises the real MemorySaver.put/get_tuple storage mechanism
    _resolve_session_start_time depends on — no mock stands in for the thing
    actually being verified.

    MemorySaver.put() pops channel_values off the checkpoint and stores each
    channel as a separate blob keyed by (thread_id, checkpoint_ns, channel,
    version) — only for channels present in BOTH new_versions (the .put()
    argument) AND the checkpoint's own channel_versions field; get_tuple()
    then reconstructs channel_values by looking up exactly those versions.
    Omitting either — as an early draft of this test did — silently drops
    the channel with no error, which is precisely the kind of persistence
    detail a mocked test would never surface."""
    ck = HybridCheckpointer(db_path=str(tmp_path / "checkpoint.sqlite"))
    ck.initialize()

    cfg: RunnableConfig = {"configurable": {"thread_id": "round-trip-thread", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["session_start_time"] = 98765.4321
    checkpoint["channel_versions"]["session_start_time"] = "1"
    ck.put(cfg, checkpoint, {}, {"session_start_time": "1"})

    tup = ck.get_tuple(cfg)
    assert tup is not None
    assert tup.checkpoint["channel_values"]["session_start_time"] == 98765.4321


# ---------------------------------------------------------------------------
# log_context_utilization — division-by-zero guard
# ---------------------------------------------------------------------------

def test_log_context_utilization_zero_budget_guard() -> None:
    from core.telemetry_log import log_context_utilization

    # Must not raise, regardless of what the underlying sink does with it.
    log_context_utilization(
        session_id="x", source="pipeline",
        total_tokens=100, token_budget=0, turn_count=1, duration_s=0.0,
    )


# ---------------------------------------------------------------------------
# Synthetic corpus — determinism + range characterization
# ---------------------------------------------------------------------------

def test_generate_corpus_determinism() -> None:
    a = generate_corpus(seeds=[1, 2, 3], turn_counts=[10, 30], context_windows=[4096])
    b = generate_corpus(seeds=[1, 2, 3], turn_counts=[10, 30], context_windows=[4096])
    assert [s.messages for s in a] == [s.messages for s in b]
    assert len(a) == 3 * 2 * 1


async def test_synthetic_corpus_range_characterization() -> None:
    """Not a GO/NO-GO assertion — proves the instrumentation fires end-to-end
    across a range of synthetic session shapes and reports the ratio
    distribution for supporting characterization in the blueprint. The
    binding GO/NO-GO signal is real telemetry, not this synthetic median."""
    corpus = generate_corpus(
        seeds=list(range(5)),
        turn_counts=[10, 30, 60],
        context_windows=[4096, 32768],
    )
    assert corpus

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "summary"

    ratios: List[float] = []
    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = mock_response
        for session in corpus:
            state = _build_state(
                num_messages=len(session.messages),
                context_window=session.context_window,
                task_id=session.session_id,
            )
            state["messages"] = session.messages
            sink: Dict[str, Any] = {}
            await _run_summarize_node_core(state, _telemetry_sink=sink)
            total = sink.get("total_tokens", 0)
            budget = sink.get("token_budget", 0)
            ratios.append((total / budget) if budget > 0 else 0.0)

    assert len(ratios) == len(corpus)
    # Reported, not asserted — the GO/NO-GO verdict is decided from real
    # telemetry (docs/PHASE_8_16_BLUEPRINT.md), not this synthetic sweep.
    median = statistics.median(ratios)
    assert 0.0 <= median <= 1.0
