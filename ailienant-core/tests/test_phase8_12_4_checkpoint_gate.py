# tests/test_phase8_12_4_checkpoint_gate.py
"""Checkpoint gate for STATE_COMPACTED live wiring (DEBT-076).

Proves that run_summarize_node fires the on_state_compacted callback when
message history is compressed, and stays silent when below the threshold.

Locked invariants:
  SC1  on_state_compacted fires once on successful LLM compression; the message
       contains "Compacted" and turns_compressed reflects the dropped turn count.
  SC2  on_state_compacted is silent when history fits within the context window
       (no-op path returns {} without touching the callback).
  SC3  functools.partial(broadcast_state_compacted, session_id) yields the
       (compaction_message, turns_compressed) arity the callback contract expects —
       regression guard against signature drift in ConnectionManager.

Test-only; sibling-gate convention. No network, no live LLM, no real WebSocket.
"""
from __future__ import annotations

import functools
import inspect
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from api.websocket_manager import ConnectionManager
from brain.summarizer import run_summarize_node

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _overflow_state(context_window: int = 500) -> dict:
    """State whose messages exceed the compaction threshold at the given window size."""
    return {
        "task_id": "sess-gate",
        "messages": [{"role": "user", "content": "word " * 200} for _ in range(20)],
        "active_llm_profile": SimpleNamespace(
            context_window=context_window,
            model_name="gpt-4",
        ),
    }


# ── SC1 ─────────────────────────────────────────────────────────────────────

async def test_sc1_state_compacted_fires_on_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: List[Tuple[str, int]] = []

    async def spy(message: str, turns: int) -> None:
        received.append((message, turns))

    async def _stub_ainvoke(*_a: Any, **_kw: Any) -> Any:
        choice = SimpleNamespace(message=SimpleNamespace(content="Summary of history."))
        return SimpleNamespace(choices=[choice])

    async def _stub_acquire(*_a: Any, **_kw: Any) -> Any:
        return SimpleNamespace(cancelled=False, holds_lock=False, effective_model="gpt-4")

    monkeypatch.setattr("tools.llm_gateway.LLMGateway.ainvoke", _stub_ainvoke)
    monkeypatch.setattr(
        "core.resource_manager.ResourceBroker.acquire_or_resolve", _stub_acquire
    )
    config: dict = {"configurable": {"on_state_compacted": spy}}
    await run_summarize_node(_overflow_state(context_window=500), config)  # type: ignore[arg-type]

    assert len(received) == 1
    assert "Compacted" in received[0][0]
    assert received[0][1] > 0


# ── SC2 ─────────────────────────────────────────────────────────────────────

async def test_sc2_state_compacted_silent_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: List[Tuple[str, int]] = []

    async def spy(message: str, turns: int) -> None:
        received.append((message, turns))

    config: dict = {"configurable": {"on_state_compacted": spy}}
    result = await run_summarize_node(
        _overflow_state(context_window=100_000), config  # type: ignore[arg-type]
    )

    assert received == []
    assert result == {}


# ── SC3 ─────────────────────────────────────────────────────────────────────

def test_sc3_partial_arity_matches_on_compacted_contract() -> None:
    sig = inspect.signature(ConnectionManager.broadcast_state_compacted)
    params = list(sig.parameters)
    # Unbound: (self, session_id, compaction_message, turns_compressed).
    # Binding vfs_manager.broadcast_state_compacted + session_id leaves
    # (compaction_message, turns_compressed) — the on_compacted ABI.
    assert "compaction_message" in params
    assert "turns_compressed" in params
    # Sanity-check that partial binds without error.
    mgr = ConnectionManager()
    cb = functools.partial(mgr.broadcast_state_compacted, "sess-test")
    cb_sig = inspect.signature(cb)
    cb_params = list(cb_sig.parameters)
    assert "compaction_message" in cb_params
    assert "turns_compressed" in cb_params
