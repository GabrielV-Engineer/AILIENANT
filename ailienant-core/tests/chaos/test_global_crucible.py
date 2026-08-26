"""Chaos Crucible: end-to-end integration tests under chaotic conditions
(network drops, double faults, phantom reconnects, mid-flight crashes).

Validates the convergence of memory compression, SQLite WAL persistence,
LangGraph orchestration, and lifecycle management under stress. The
topology-selector suite this file originally also covered (SEQUENTIAL /
MICRO_SWARM / FULL_SWARM dispatch and the deterministic syntax/style gates)
was removed with the dead modules it exercised — they had no production
caller (the main graph in brain/engine.py never dispatched into them).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.anyio


# =====================================================================
# A. Memory Stress
# =====================================================================


async def test_summarizer_protects_phase4_state() -> None:
    """The message Summarizer (run_summarize_node, brain/summarizer.py) must
    compress oversized `messages` while leaving unrelated control channels
    (error_streak, active_role, circuit_breaker_tripped, cloud_surgeon_invocations)
    untouched."""
    from brain.state import LLMProfile
    from brain.summarizer import run_summarize_node

    big_chunk = "x" * 1000
    messages = [{"role": "assistant", "content": big_chunk} for _ in range(50)]

    control_fields = {
        "error_streak": 2,
        "active_role": "core_dev",
        "circuit_breaker_tripped": False,
        "cloud_surgeon_invocations": 0,
        "style_bypass_active": False,
    }

    state = {
        "messages": messages,
        "task_id": "chaos-a2",
        "active_llm_profile": LLMProfile(
            model_name="gpt-4",
            parameters_b=0.0,
            context_window=2048,
            quantization="fp16",
        ),
        **control_fields,
    }

    fake_response = type(
        "R",
        (),
        {
            "choices": [
                type(
                    "C",
                    (),
                    {"message": type("M", (), {"content": "compressed history"})()},
                )()
            ]
        },
    )()

    fake_decision = type(
        "D",
        (),
        {"cancelled": False, "effective_model": "small", "holds_lock": False},
    )()

    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new_callable=AsyncMock,
        return_value=fake_response,
    ), patch(
        "brain.summarizer.ResourceBroker.acquire_or_resolve",
        new_callable=AsyncMock,
        return_value=fake_decision,
    ):
        delta = await run_summarize_node(state)

    # Compression fired: __replace__ sentinel present and shorter than input.
    assert "messages" in delta, "Summarizer should have compressed messages."
    assert isinstance(delta["messages"][0], dict) and delta["messages"][0].get(
        "__replace__"
    )
    assert len(delta["messages"]) < len(messages)

    # Unrelated control fields are NOT in the delta — Summarizer never writes them.
    for key in control_fields:
        assert key not in delta, f"Summarizer leaked control field: {key}"


# =====================================================================
# B. Lifecycle Phantom Reconnects
# =====================================================================


async def test_lifecycle_debounce_prevents_vram_purge() -> None:
    """shutdown_workspace schedules VRAM purge after debounce_sec.
    If register_task is called for the same PID within the window, the timer
    is cancelled and _release_vram is NEVER called.
    """
    from core.lifecycle_manager import WorkspaceLifecycleManager

    mgr = WorkspaceLifecycleManager(debounce_sec=0.05)

    with patch.object(mgr, "_release_vram", new_callable=AsyncMock) as release_mock:
        await mgr.shutdown_workspace(4242)

        # Phantom reconnect well within the debounce window.
        await asyncio.sleep(0.01)
        loop_task = asyncio.create_task(asyncio.sleep(0))
        mgr.register_task(4242, loop_task)

        # Wait past the original window. The TimerHandle should be cancelled.
        await asyncio.sleep(0.10)
        await loop_task

    assert release_mock.await_count == 0, (
        f"Debounce failed: _release_vram called {release_mock.await_count} times "
        "despite reconnect within window."
    )
