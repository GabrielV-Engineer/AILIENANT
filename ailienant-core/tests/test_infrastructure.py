# ailienant-core/tests/test_infrastructure.py
#
# DoD: pytest tests/test_infrastructure.py -v must pass with 0 failures.

import asyncio
import math
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import _merge_messages
from brain.summarizer import KEEP_LAST_N, THRESHOLD_RATIO, run_summarize_node
from core.io_coalescer import IOCoalescer, is_critical_file
from shared.config import MODEL_SMALL


# ---------------------------------------------------------------------------
# _merge_messages reducer — unit tests (synchronous)
# ---------------------------------------------------------------------------

def test_merge_messages_normal_append() -> None:
    old = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    result = _merge_messages(old, [{"role": "user", "content": "c"}])
    assert result == old + [{"role": "user", "content": "c"}]


def test_merge_messages_sentinel_replaces() -> None:
    old = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    replacement = [{"role": "system", "content": "summary"}]
    result = _merge_messages(old, [{"__replace__": True}, *replacement])
    assert result == replacement


def test_merge_messages_empty_update_is_append() -> None:
    old = [{"role": "user", "content": "a"}]
    assert _merge_messages(old, []) == old


def test_merge_messages_sentinel_only_produces_empty_list() -> None:
    old = [{"role": "user", "content": "a"}]
    result = _merge_messages(old, [{"__replace__": True}])
    assert result == []


# ---------------------------------------------------------------------------
# StateSummarizer — async tests
# ---------------------------------------------------------------------------

def _build_state(num_messages: int, context_window: int = 8192) -> dict:
    from brain.state import LLMProfile
    return {
        "task_id": "test-task",
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message content {i}"}
            for i in range(num_messages)
        ],
        "active_llm_profile": LLMProfile(
            model_name="gpt-4",
            parameters_b=7.0,
            context_window=context_window,
            quantization="q4",
        ),
    }


@pytest.mark.anyio
async def test_summarizer_no_op_when_few_messages() -> None:
    state = _build_state(KEEP_LAST_N)
    result = await run_summarize_node(state)
    assert result == {}


@pytest.mark.anyio
async def test_summarizer_no_op_when_within_budget() -> None:
    # Large context window — 100 short messages won't exceed 80% budget
    state = _build_state(100, context_window=200_000)
    result = await run_summarize_node(state)
    assert result == {}


@pytest.mark.anyio
async def test_summarizer_compresses_when_over_threshold() -> None:
    # Force threshold breach: tiny context_window so even a few messages exceed 80%
    state = _build_state(20, context_window=10)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "This is the summary."

    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = mock_response
        result = await run_summarize_node(state)

    assert "messages" in result
    output_messages: List[Dict] = result["messages"]

    # First element must be the __replace__ sentinel
    assert output_messages[0].get("__replace__") is True

    # After applying the reducer, result should be: 1 summary + KEEP_LAST_N recent
    final = _merge_messages(state["messages"], output_messages)
    assert len(final) == KEEP_LAST_N + 1
    assert final[0]["role"] == "system"
    assert "[HISTORY SUMMARY]" in final[0]["content"]
    # Last KEEP_LAST_N messages must be the tail of the original list
    assert final[1:] == state["messages"][-KEEP_LAST_N:]


@pytest.mark.anyio
async def test_summarizer_uses_model_small() -> None:
    state = _build_state(20, context_window=10)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Summary."

    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = mock_response
        await run_summarize_node(state)

    call_kwargs = mock_invoke.call_args.kwargs
    assert call_kwargs.get("model") == MODEL_SMALL


@pytest.mark.anyio
async def test_summarizer_truncates_on_llm_failure() -> None:
    state = _build_state(20, context_window=10)

    with patch("brain.summarizer.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.side_effect = RuntimeError("LLM unreachable")
        result = await run_summarize_node(state)

    assert "messages" in result
    output_messages = result["messages"]
    assert output_messages[0].get("__replace__") is True

    # Fallback: no summary message, only the sentinel + recent
    final = _merge_messages(state["messages"], output_messages)
    assert len(final) == KEEP_LAST_N
    # No system summary in the fallback
    assert not any("[HISTORY SUMMARY]" in m.get("content", "") for m in final)


# ---------------------------------------------------------------------------
# IOCoalescer — async tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_coalescer_deduplicates_same_file() -> None:
    """50 saves for the same file → exactly 1 dispatch with the last content."""
    dispatch_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append((fp, content))

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)

    for i in range(50):
        coalescer.submit("test.py", f"v{i}", "")

    # Wait past the debounce window
    await asyncio.sleep(0.7)

    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][0] == "test.py"
    assert dispatch_calls[0][1] == "v49"  # last content wins


@pytest.mark.anyio
async def test_coalescer_dispatches_50_unique_files() -> None:
    """50 unique files submitted → 50 dispatch calls, but all from 1 flush batch."""
    dispatch_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append(fp)

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)

    for i in range(50):
        coalescer.submit(f"file_{i}.py", f"content_{i}", "")

    await asyncio.sleep(0.7)

    assert len(dispatch_calls) == 50


@pytest.mark.anyio
async def test_coalescer_debounce_resets_on_new_submit() -> None:
    """Submit at t=0, submit again at t=300ms; dispatch fires only once at ~800ms."""
    dispatch_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append((fp, content))

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)

    coalescer.submit("a.py", "v1", "")
    await asyncio.sleep(0.3)
    # Re-submit before debounce expires — resets the timer
    coalescer.submit("a.py", "v2", "")
    # At this point only 300ms have passed; dispatch should NOT have fired yet
    assert len(dispatch_calls) == 0

    await asyncio.sleep(0.6)
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][1] == "v2"


@pytest.mark.anyio
async def test_coalescer_no_dispatch_without_registered_fn() -> None:
    """Submitting without register_dispatch must not raise."""
    coalescer = IOCoalescer()
    coalescer.submit("test.py", "content", "")
    await asyncio.sleep(0.7)
    # No exception raised — test passes implicitly


# ---------------------------------------------------------------------------
# is_critical_file — unit tests (synchronous)
# ---------------------------------------------------------------------------

def test_is_critical_env_file() -> None:
    assert is_critical_file("/project/.env") is True
    assert is_critical_file("/project/.env.local") is True
    assert is_critical_file("/project/.env.production") is True


def test_is_critical_config_files() -> None:
    assert is_critical_file("/project/config.py") is True
    assert is_critical_file("/project/settings.py") is True
    assert is_critical_file("/project/secrets.py") is True


def test_is_not_critical_regular_file() -> None:
    assert is_critical_file("/project/main.py") is False
    assert is_critical_file("/project/utils.ts") is False
    assert is_critical_file("/project/README.md") is False
