"""Mid-run operator steering — checkpoint gate.

Steering exists so "investigá esta URL y seguí" has a path that is neither a
rejected submit nor an abort that discards the run. The load-bearing constraint is
that it must not reopen the concurrency defect DEBT-170 closed: a submit spawns a
runner, and a second runner on one checkpoint is what that fix rejects. Steering
spawns nothing — it writes to a queue the EXISTING runner drains.

Rows
  STEER1  a steering message reaches the model as a `user` turn
  STEER2  submit still rejects while busy; steering starts no second runner
  STEER3  a replayed super-step injects the same message exactly once
  STEER4  admission: not-busy, duplicate, and queue-full are refused
  STEER5  the operator's own text is never quarantine-wrapped
  STEER6  the governor sees the grant and still bounds it
  STEER7  the queue is dropped when the runner finishes
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

import brain.agentic_cell as ac
from shared.config import (
    STEERING_ITERATION_GRANT,
    STEERING_MAX_CHARS,
    STEERING_MAX_QUEUED,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubService:
    """Minimal stand-in for the TaskService seam `drain_steering` reads."""

    def __init__(self, queued: List[Tuple[str, str]]) -> None:
        self._queued = queued

    def peek_steering(self, _session_id: str) -> List[Tuple[str, str]]:
        return list(self._queued)


def _with_service(monkeypatch: pytest.MonkeyPatch, queued: List[Tuple[str, str]]) -> None:
    import core.task_service as ts

    monkeypatch.setattr(ts, "get_task_service", lambda: _StubService(queued))


# =====================================================================
# STEER1 / STEER5 — the message reaches the model, unwrapped
# =====================================================================


def test_steer1_steering_reaches_the_model_as_a_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_service(monkeypatch, [("m1", "also read https://example.com/spec")])
    state: Dict[str, Any] = {"task_id": "s1", "user_input": "build it"}

    records, consumed = ac.drain_steering(state)
    assert consumed == ["m1"]
    assert records == [{"role": "user", "content": "also read https://example.com/spec"}]

    # And the replay actually carries a `user` record — it used to drop anything
    # that was neither `system` nor a diagnostics row.
    state["agentic_trajectory"] = records
    messages = ac._build_messages(state)
    assert any(
        m["role"] == "user" and "example.com/spec" in m["content"] for m in messages
    ), "a user-role trajectory record never reached the model"


def test_steer5_operator_text_is_not_quarantine_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapping the operator's own instruction would tell the model to ignore it.

    Quarantine marks UNTRUSTED content inert. A steering message is the operator
    speaking; a URL inside it is still quarantined once `web_fetch` returns the
    page, which is the boundary that actually matters.
    """
    text = "stop using the old API and switch to v2"
    _with_service(monkeypatch, [("m1", text)])
    records, _ = ac.drain_steering({"task_id": "s1"})
    assert records[0]["content"] == text
    assert "<" not in records[0]["content"]


# =====================================================================
# STEER3 — replay-safety
# =====================================================================


def test_steer3_watermark_makes_a_replayed_drain_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading is non-destructive; the watermark is what prevents a double-inject.

    A node that popped the queue and then failed before its delta committed would
    lose the message — the hazard `pending_brief` and DEBT-129 guard against. So
    the queue is re-read on every super-step and filtered against state instead.
    """
    queued = [("m1", "first"), ("m2", "second")]
    _with_service(monkeypatch, queued)

    first_records, first_consumed = ac.drain_steering({"task_id": "s1"})
    assert first_consumed == ["m1", "m2"]

    # Same queue, now with the watermark the first pass produced: nothing repeats.
    replay_state = {"task_id": "s1", "_consumed_steering_ids": first_consumed}
    second_records, second_consumed = ac.drain_steering(replay_state)
    assert second_records == [] and second_consumed == []

    # A message that arrives after the watermark is still picked up.
    queued.append(("m3", "third"))
    third_records, third_consumed = ac.drain_steering(replay_state)
    assert third_consumed == ["m3"]
    assert third_records == [{"role": "user", "content": "third"}]
    assert len(first_records) == 2


def test_steer3b_a_drain_fault_never_breaks_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's aside must not be able to fail the work it redirects."""
    import core.task_service as ts

    def _boom() -> Any:
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(ts, "get_task_service", _boom)
    assert ac.drain_steering({"task_id": "s1"}) == ([], [])
    # No session at all is the other degenerate case.
    assert ac.drain_steering({}) == ([], [])


# =====================================================================
# STEER2 / STEER4 / STEER7 — admission and lifecycle
# =====================================================================


def _service() -> Any:
    from core.task_service import TaskService

    return TaskService.__new__(TaskService)


def _armed_service(busy: bool) -> Any:
    svc = _service()
    svc._steering_queues = {}
    svc.is_session_busy = lambda _sid: busy  # type: ignore[method-assign]
    return svc


def test_steer4_admission_refuses_not_busy_duplicate_and_full() -> None:
    idle = _armed_service(busy=False)
    assert idle.enqueue_steering("s1", "m1", "hi") == "not_busy", (
        "with nothing running the operator should submit normally, not steer"
    )

    svc = _armed_service(busy=True)
    assert svc.enqueue_steering("s1", "m1", "hi") == "queued"
    assert svc.enqueue_steering("s1", "m1", "hi again") == "duplicate", (
        "a retried message_id must not inject the same instruction twice"
    )

    for i in range(STEERING_MAX_QUEUED - 1):
        assert svc.enqueue_steering("s1", f"extra{i}", "x") == "queued"
    assert svc.enqueue_steering("s1", "overflow", "x") == "full"
    assert len(svc.peek_steering("s1")) == STEERING_MAX_QUEUED


def test_steer4b_oversized_text_is_truncated_not_rejected() -> None:
    svc = _armed_service(busy=True)
    assert svc.enqueue_steering("s1", "m1", "x" * (STEERING_MAX_CHARS * 3)) == "queued"
    (_mid, text), = svc.peek_steering("s1")
    assert len(text) == STEERING_MAX_CHARS


def test_steer7_queue_is_dropped_for_a_finished_session() -> None:
    svc = _armed_service(busy=True)
    svc.enqueue_steering("s1", "m1", "hi")
    assert svc.peek_steering("s1")
    svc.drop_steering_queue("s1")
    assert svc.peek_steering("s1") == []


@pytest.mark.anyio
async def test_steer2_steering_starts_no_second_runner_and_submit_still_rejects() -> None:
    """The DEBT-170 regression lock.

    That defect was a second `asyncio.create_task(_runner())` against one
    checkpoint. Steering must remain a queue write — asserted structurally, by
    reading the source, because the failure mode is a spawn that would look
    perfectly healthy in any single-turn behavioural test.
    """
    import inspect

    from core.task_service import TaskService

    source = inspect.getsource(TaskService.enqueue_steering)
    for spawner in ("create_task", "ensure_future", "_runner", "process_task"):
        assert spawner not in source, (
            f"enqueue_steering references {spawner!r} — steering must never start work"
        )

    # And the admission guard it depends on is still the submit-side reject.
    assert "is_session_busy" in source

    svc = _armed_service(busy=True)
    before = asyncio.all_tasks()
    svc.enqueue_steering("s1", "m1", "hi")
    assert asyncio.all_tasks() == before


# =====================================================================
# STEER6 — the governor sees the grant, and it stays bounded
# =====================================================================


def test_steer6_grant_is_proportional_and_bounded() -> None:
    """New work asked for mid-run has to be funded, but not without limit."""
    assert ac._steering_step_grant({}, []) == 0

    one = ac._steering_step_grant({}, ["m1"])
    assert one == STEERING_ITERATION_GRANT

    # Derived from the TOTAL consumed, so the grant survives the iterations it
    # funds rather than evaporating on the next super-step.
    carried = ac._steering_step_grant({"_consumed_steering_ids": ["m1"]}, [])
    assert carried == STEERING_ITERATION_GRANT

    # Same id twice never pays twice.
    assert ac._steering_step_grant({"_consumed_steering_ids": ["m1"]}, ["m1"]) == one

    # The ceiling: even a full queue cannot buy an unbounded run.
    full = ac._steering_step_grant(
        {"_consumed_steering_ids": [f"m{i}" for i in range(STEERING_MAX_QUEUED)]}, []
    )
    assert full == STEERING_MAX_QUEUED * STEERING_ITERATION_GRANT


def test_steer6b_contract_is_registered_and_carries_a_dedup_key() -> None:
    import typing

    from api.ws_contracts import ClientSteeringMessageEvent, WebSocketMessage

    # The master contract is a tagged Union; membership is what makes an inbound
    # message parse at all, so an unregistered event is silently unroutable.
    assert ClientSteeringMessageEvent in typing.get_args(WebSocketMessage)
    event = ClientSteeringMessageEvent.model_validate(
        {
            "event_type": "client_steering_message",
            "data": {"session_id": "s1", "message_id": "m1", "text": "go"},
        }
    )
    assert event.data.message_id == "m1"

    with pytest.raises(Exception):
        ClientSteeringMessageEvent.model_validate(
            {
                "event_type": "client_steering_message",
                "data": {
                    "session_id": "s1",
                    "message_id": "m1",
                    "text": "x" * (STEERING_MAX_CHARS + 1),
                },
            }
        )
