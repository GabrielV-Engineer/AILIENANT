# tests/test_token_batcher.py
"""Phase 7.10.2 DoD — Token batcher + narration bandwidth gate (ADR-702).

Seven tests:
  T1. burst coalesces into a single frame   — no token loss; trailing flush works.
  T2. size cap flushes before the window    — max_buffer_chars bounds memory.
  T3. timing window (DoD mock transmitter)  — sub-window tokens emerge as ~40ms-spaced
       coalesced blocks (DOM-thrash neutralization), no loss.
  T4. NarrationGate cold start              — pre-answer narration is never suppressed.
  T5. NarrationGate enforcement             — narration <= 15% once the answer streams.
  T6. NarrationGate transition              — record_answer flips cold-start into enforcement.
  T7. _run_coding_task ordering             — granular sub-steps fire in order, coder N/M.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep
from core.task_service import TaskPayload, TaskService
from transport.token_batcher import (
    MAX_NARRATION_RATIO,
    NarrationGate,
    batch_tokens,
)


async def _source(tokens: List[str], delay_s: float) -> AsyncIterator[str]:
    """Async token producer; ``delay_s`` between emissions simulates stream cadence."""
    for tok in tokens:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        yield tok


# ── T1 — instant burst coalesces into one frame (no loss, trailing flush) ─────


@pytest.mark.anyio
async def test_batch_tokens_burst_coalesces_into_single_frame() -> None:
    tokens = [f"t{i}" for i in range(50)]
    out = [block async for block in batch_tokens(_source(tokens, 0.0), chunk_ms=1000)]

    # No loss / order preserved.
    assert "".join(out) == "".join(tokens)
    # All deltas arrived inside one window → one coalesced frame (trailing flush).
    assert len(out) == 1


# ── T2 — size cap flushes before the time window ─────────────────────────────


@pytest.mark.anyio
async def test_batch_tokens_flushes_on_size_cap() -> None:
    chunk = "x" * 50
    tokens = [chunk] * 10  # 500 chars total, cap = 100
    # chunk_ms huge so the timer never trips — only the size cap can split frames.
    out = [
        block
        async for block in batch_tokens(
            _source(tokens, 0.0), chunk_ms=100_000, max_buffer_chars=100
        )
    ]

    assert "".join(out) == chunk * 10  # no loss
    assert len(out) >= 2               # the size cap produced multiple frames


# ── T3 — timing window: DoD-required mock-transmitter coalescing ─────────────


@pytest.mark.anyio
async def test_mock_transmitter_receives_coalesced_blocks_near_40ms() -> None:
    """Sub-window tokens must reach the transmitter as ~40ms-spaced coalesced blocks."""
    loop = asyncio.get_running_loop()
    received: List[Tuple[float, str]] = []

    tokens = [f"x{i}" for i in range(40)]
    async for block in batch_tokens(_source(tokens, 0.003), chunk_ms=40):
        received.append((loop.time(), block))

    # Coalesced — far fewer frames than tokens (DOM-thrash neutralized).
    assert 2 <= len(received) < len(tokens)
    # No loss.
    assert "".join(block for _, block in received) == "".join(tokens)
    # Inter-frame gaps for the TIME-DRIVEN flushes are >= the window by construction
    # (window_start resets to loop.time() right after each yield, so the next flush
    # cannot fire until another full window elapses). A 30ms floor absorbs clock
    # granularity on slow/Windows CI. The final frame is excluded: it is the trailing
    # partial-buffer flush fired at stream end, not on the timer, so its gap may be < 40ms.
    gaps = [received[i][0] - received[i - 1][0] for i in range(1, len(received))]
    time_driven_gaps = gaps[:-1]
    assert all(gap >= 0.030 for gap in time_driven_gaps)


# ── T4 — NarrationGate cold start: pre-answer narration always allowed ───────


def test_narration_gate_cold_start_allows_all() -> None:
    gate = NarrationGate()
    # No answer recorded yet (context_gather → routing → drafting): never suppress.
    for _ in range(100):
        assert gate.allow(50) is True
    assert gate.answer_bytes == 0
    assert gate.narration_bytes == 0  # cold-start packets are uncharged


# ── T5 — NarrationGate enforcement once the answer channel is live ───────────


def test_narration_gate_enforces_15pct_when_streaming() -> None:
    gate = NarrationGate()
    gate.record_answer(1000)  # answer now streaming

    accepted = sum(1 for _ in range(1000) if gate.allow(1))

    total = gate.answer_bytes + gate.narration_bytes
    assert gate.narration_bytes / total <= MAX_NARRATION_RATIO + 1e-9
    assert accepted > 0           # not a hard block — some narration gets through
    assert accepted < 1000        # but it is bounded


# ── T6 — NarrationGate transition: record_answer flips the gate ──────────────


def test_narration_gate_transitions_cold_to_enforced() -> None:
    gate = NarrationGate()
    assert gate.allow(10_000) is True   # cold start: unbounded, uncharged

    gate.record_answer(100)             # answer goes live → enforcement begins
    assert gate.allow(5) is True        # small packet fits within 15% of 100
    assert gate.allow(10_000) is False  # oversized packet rejected


# ── T7 — _run_coding_task emits granular sub-steps in order (context → N/M) ───


def _mission_two_tasks() -> MissionSpecification:
    return MissionSpecification(
        outcome="Two-step change.",
        scope=["a.py", "b.py"],
        constraints=["none"],
        decisions=["go"],
        tasks=[
            WBSStep(step_number=1, target_role="core_dev", action="edit_file",
                    target_file="a.py", description="step one"),
            WBSStep(step_number=2, target_role="core_dev", action="edit_file",
                    target_file="b.py", description="step two"),
        ],
        checks=["ok"],
    )


@pytest.mark.anyio
async def test_run_coding_task_emits_granular_narration_in_order() -> None:
    async def _fake_planner(state: dict) -> dict:
        # Simulate the real planner narrating its phases via the injected emitter.
        narrate = state.get("narrate")
        if narrate is not None:
            await narrate("routing_decision")
            await narrate("drafting_spec")
        return {"mission_spec": _mission_two_tasks()}

    pipeline = AsyncMock()  # captures broadcast_pipeline_step(session_id, node_name, step_id)
    ctxs = [
        patch("agents.planner.run_planner_node", new=AsyncMock(side_effect=_fake_planner)),
        patch("agents.coder.run_coder_node", new=AsyncMock(return_value={})),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=pipeline),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
    ]
    for c in ctxs:
        c.start()
    try:
        payload = TaskPayload(task_prompt="do two things", dirty_buffers=[], project_id=None)
        await TaskService()._run_coding_task("s7", payload, "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    node_names = [call.args[1] for call in pipeline.await_args_list]
    assert node_names == [
        "context_gather",
        "routing_decision",
        "drafting_spec",
        "coder_agent (1/2)",
        "coder_agent (2/2)",
    ]
