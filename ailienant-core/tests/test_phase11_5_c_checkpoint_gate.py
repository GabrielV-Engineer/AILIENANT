"""Phase 11.5.C — Agent Activity Timeline Checkpoint Gate.

Single certification that the Glass-Box Timeline backend contract holds
against the shipped entry points. Does NOT re-run the detailed rows in
``test_activity_channel.py`` (classifier vocabulary, order-agnostic body
correlation, reasoning-span ref threading) or ``test_token_batcher.py``
(NarrationGate's own ratio-limiting, unaffected); each row here pins ONE
architectural invariant that a future refactor could accidentally remove.

Gate rows certified:

  UNTHROTTLE1  the activity channel is genuinely un-throttled — a burst of
               narration that would exceed NarrationGate's 15% ratio still
               delivers every event on `server_activity_event`.
  SEQ1         `seq` is a strictly increasing, gap-free, per-turn counter.
  ENUM1        `ActivityEventPayload.kind` rejects any value outside the
               closed `ActivityKind` vocabulary (contract-level pin, not
               classifier vocabulary — see test_activity_channel.py for that).
  CAP1         past `ACTIVITY_CAP` events, a single sentinel replaces the
               rest — bounded, not silently truncated and not unbounded.
  ADDITIVE1    the payload's non-required fields default to None and the
               event round-trips through model_dump/model_validate losslessly
               — an older client tolerates an event it doesn't fully use.
  NARRATE1     `_narrate` never calls the legacy `broadcast_pipeline_step`
               channel — the 11.5.C.3 retirement (PipelineProgress's only
               consumer was replaced by AgentTimeline in 11.5.C.2).

All async cases run under anyio (asyncio backend).
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, cast
from unittest.mock import AsyncMock, patch

import pytest

from api.ws_contracts import ActivityEventPayload, ServerActivityEvent
from brain.state import MissionSpecification, WBSStep
from core.task_service import TaskPayload, TaskService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _mission() -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.", scope=["calc.py"], constraints=["none"],
        decisions=["go"],
        tasks=[WBSStep(
            step_number=1, target_role="core_dev", action="edit_file",
            target_file="calc.py", description="bump", status="pending",
        )],
        checks=["ok"],
    )


async def _run_with_narrate_burst(n: int, answer_bytes: int) -> List[Dict[str, Any]]:
    """Drive `_run_coding_task` with a fake graph that narrates `n` classifiable
    events, plus a simulated answer stream large enough to have tripped
    NarrationGate's 15% ratio under the retired server_pipeline_step gating.
    Returns the captured broadcast_activity_event call kwargs, in call order."""
    captured: List[Dict[str, Any]] = []

    async def _capture(session_id: str, **kwargs: Any) -> None:
        captured.append(kwargs)

    def _fake_astream(state: dict, *_a: object, **_k: object) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            config = cast(Dict[str, Any], _k.get("config") or {})
            narrate = config.get("configurable", {}).get("narrate")
            if narrate is not None:
                for _ in range(n):
                    await narrate("critic_review")  # classifies to kind="reviewing"
            yield {"mission_spec": _mission()}

        return _gen()

    async def _fake_broadcast_token(session_id: str, text: str) -> None:
        # Simulates a large answer stream — enough that the retired ratio gate
        # (narration / (answer + narration) <= 15%) would have suppressed most
        # of the `n` narration calls above had it still governed this channel.
        pass

    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.task_service.vfs_manager.broadcast_activity_event", new=AsyncMock(side_effect=_capture)),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock(side_effect=_fake_broadcast_token)),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=AsyncMock(return_value={"approved": False})),
    ]
    for c in ctxs:
        c.start()
    try:
        payload = TaskPayload(task_prompt="burst test", dirty_buffers=[], project_id=None)
        await TaskService()._run_coding_task("gate-11-5-c", payload, "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()
    return captured


# ── UNTHROTTLE1 ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_UNTHROTTLE1_activity_channel_delivers_every_event() -> None:
    N = 30
    captured = await _run_with_narrate_burst(n=N, answer_bytes=50_000)
    # "understanding" (context_gather) + N "reviewing" (critic_review) events —
    # ALL delivered despite a large simulated answer stream, unlike the retired
    # server_pipeline_step channel this superseded.
    reviewing = [c for c in captured if c["kind"] == "reviewing"]
    assert len(reviewing) == N


# ── SEQ1 ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_SEQ1_seq_is_strictly_increasing_and_gapfree() -> None:
    captured = await _run_with_narrate_burst(n=10, answer_bytes=0)
    seqs = [c["seq"] for c in captured]
    assert seqs == list(range(len(seqs)))


# ── ENUM1 ────────────────────────────────────────────────────────────────


def test_ENUM1_payload_rejects_unknown_kind() -> None:
    # A recognized kind constructs fine…
    ActivityEventPayload(session_id="s", seq=0, ts=0.0, kind="read")
    # …an unrecognized one is rejected by the closed Literal vocabulary.
    with pytest.raises(Exception):
        ActivityEventPayload(session_id="s", seq=0, ts=0.0, kind="not_a_real_kind")  # type: ignore[arg-type]


# ── CAP1 ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_CAP1_past_the_cap_a_single_sentinel_replaces_the_rest() -> None:
    import core.task_service as ts_mod
    with patch.object(ts_mod, "ACTIVITY_CAP", 3):
        captured = await _run_with_narrate_burst(n=10, answer_bytes=0)
    # understanding(seq0) consumes the first slot, leaving 2 allowed reviewing
    # events (seq1-2) before seq3 == ACTIVITY_CAP trips the sentinel — 4 total.
    # The remaining 7 reviewing calls (seq4-10) are dropped, not queued/delayed.
    assert len(captured) == 4
    sentinel = captured[-1]
    assert sentinel["kind"] == "command"
    assert sentinel["metric"] is not None and "capped at 3" in sentinel["metric"]
    reviewing = [c for c in captured if c["kind"] == "reviewing"]
    assert len(reviewing) == 2


# ── ADDITIVE1 ────────────────────────────────────────────────────────────


def test_ADDITIVE1_optional_fields_default_and_roundtrip() -> None:
    payload = ActivityEventPayload(session_id="s1", seq=0, ts=1.0, kind="plan")
    assert payload.target is None
    assert payload.metric is None
    assert payload.ref is None

    event = ServerActivityEvent(data=payload)
    dumped = event.model_dump(mode="json")
    assert dumped["event_type"] == "server_activity_event"
    restored = ServerActivityEvent.model_validate(dumped)
    assert restored == event


# ── NARRATE1 ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_NARRATE1_narrate_never_calls_the_retired_pipeline_step_channel() -> None:
    pipeline_step = AsyncMock()

    def _fake_astream(state: dict, *_a: object, **_k: object) -> AsyncIterator[dict]:
        async def _gen() -> AsyncIterator[dict]:
            config = cast(Dict[str, Any], _k.get("config") or {})
            narrate = config.get("configurable", {}).get("narrate")
            if narrate is not None:
                await narrate("drafting_spec")
            yield {"mission_spec": _mission()}

        return _gen()

    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.task_service.vfs_manager.broadcast_activity_event", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=pipeline_step),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=AsyncMock(return_value={"approved": False})),
    ]
    for c in ctxs:
        c.start()
    try:
        payload = TaskPayload(task_prompt="narrate retire check", dirty_buffers=[], project_id=None)
        await TaskService()._run_coding_task("gate-narrate1", payload, "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    pipeline_step.assert_not_awaited()
