"""ConnectionManager's §43 devcontainer interactive-session primitives.

Exercises the manager's own state machine — queue registration, the push/drain
backpressure transition detector, idempotent unregistration, and the
disconnect reap — directly, without a `_BridgeSandboxSession` in the loop.
`api.websocket_manager.ConnectionManager` is safe to construct standalone (no
FastAPI lifespan) and `send_personal_message` is a documented no-op for a
`client_id` with no live connection, so these tests exercise real production
code, not a hand-rolled fake of it.
"""
from __future__ import annotations

import asyncio
import base64
import json

from api.websocket_manager import (
    ConnectionManager,
    _DEVC_SESSION_QUEUE_HIGH_WATER,
    _DEVC_SESSION_QUEUE_LOW_WATER,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _open_session(mgr: ConnectionManager, session_id: str, session_ref: str) -> None:
    """Drive the open handshake to completion so a queue is registered."""
    task = asyncio.ensure_future(
        mgr.wait_devcontainer_session_opened(
            session_ref=session_ref, session_id=session_id, timeout=5.0,
        )
    )
    await asyncio.sleep(0)  # let wait_devcontainer_session_opened register the queue
    mgr.resolve_devcontainer_session_opened(session_ref, True, None)
    result = await task
    assert result == {"ok": True, "detail": None}


# ── wire contract round-trip ──────────────────────────────────────────────────


def test_all_eight_session_events_parse_through_the_discriminated_union() -> None:
    """A discriminator/field mismatch in any of the eight §43 models would
    otherwise only surface at runtime, the first time a real frame arrives —
    this exercises the exact `validate_incoming` path `main.py`'s receive
    loop uses, for every event this contract adds."""
    mgr = ConnectionManager()
    payloads = [
        {"event_type": "server_devcontainer_session_open",
         "data": {"session_id": "s1", "session_ref": "r1", "cwd": "/w", "env_keys": ["CI"]}},
        {"event_type": "client_devcontainer_session_opened",
         "data": {"session_id": "s1", "session_ref": "r1", "ok": True, "detail": None}},
        {"event_type": "client_devcontainer_session_opened",
         "data": {"session_id": "s1", "session_ref": "r1", "ok": False, "detail": "no config"}},
        {"event_type": "server_devcontainer_session_stdin",
         "data": {"session_id": "s1", "session_ref": "r1", "data_b64": "aGk="}},
        {"event_type": "server_devcontainer_session_signal",
         "data": {"session_id": "s1", "session_ref": "r1", "signal": "interrupt"}},
        {"event_type": "server_devcontainer_session_flow",
         "data": {"session_id": "s1", "session_ref": "r1", "paused": True}},
        {"event_type": "server_devcontainer_session_close",
         "data": {"session_id": "s1", "session_ref": "r1"}},
        {"event_type": "client_devcontainer_session_stream",
         "data": {"session_id": "s1", "session_ref": "r1", "chunk_b64": "aGVsbG8="}},
        {"event_type": "client_devcontainer_session_exit",
         "data": {"session_id": "s1", "session_ref": "r1", "exit_code": 0}},
    ]

    async def scenario() -> None:
        for payload in payloads:
            result = await mgr.validate_incoming(json.dumps(payload))
            assert result is not None, f"failed to parse: {payload}"
            assert result.event_type == payload["event_type"]

    asyncio.run(scenario())


# ── open handshake ────────────────────────────────────────────────────────────


def test_open_registers_queue_on_success() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        assert mgr.get_devcontainer_session_queue("r1") is not None

    asyncio.run(scenario())


def test_open_failure_tears_down_the_queue_it_registered() -> None:
    """A queue is registered BEFORE the open reply arrives (so a racing chunk
    is never lost); on ok=False it must not be left behind."""
    mgr = ConnectionManager()

    async def scenario() -> None:
        task = asyncio.ensure_future(
            mgr.wait_devcontainer_session_opened(session_ref="r1", session_id="s1", timeout=5.0)
        )
        await asyncio.sleep(0)
        assert mgr.get_devcontainer_session_queue("r1") is not None  # registered up front
        mgr.resolve_devcontainer_session_opened("r1", False, "no devcontainer.json")
        result = await task
        assert result == {"ok": False, "detail": "no devcontainer.json"}
        assert mgr.get_devcontainer_session_queue("r1") is None  # torn down on failure

    asyncio.run(scenario())


def test_open_timeout_tears_down_the_queue_and_returns_none() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        result = await mgr.wait_devcontainer_session_opened(
            session_ref="r1", session_id="s1", timeout=0.05,
        )
        assert result is None
        assert mgr.get_devcontainer_session_queue("r1") is None

    asyncio.run(scenario())


# ── push/drain backpressure transitions ──────────────────────────────────────


def test_push_below_high_water_reports_no_transition() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        for _ in range(10):
            assert mgr.push_devcontainer_session_chunk("r1", _b64(b"x")) is None

    asyncio.run(scenario())


def test_push_crossing_high_water_pauses_exactly_once() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        transitions = [
            mgr.push_devcontainer_session_chunk("r1", _b64(b"x"))
            for _ in range(_DEVC_SESSION_QUEUE_HIGH_WATER + 5)
        ]
        # Exactly one True (the crossing), everything else None.
        assert transitions.count(True) == 1
        assert all(t in (None, True) for t in transitions)

    asyncio.run(scenario())


def test_drain_below_low_water_resumes_even_with_no_further_push() -> None:
    """The deadlock case this design must not have: once paused and the
    producer stops entirely (the child is stalled writing to its own stdout
    pipe because the host never resumed), only a CONSUMER-side check can ever
    observe the drain and signal resume."""
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        for _ in range(_DEVC_SESSION_QUEUE_HIGH_WATER):
            mgr.push_devcontainer_session_chunk("r1", _b64(b"x"))
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None

        # No more pushes from here — simulate the producer having stalled.
        resumed_at = None
        drained = 0
        while queue.qsize() > 0:
            queue.get_nowait()
            drained += 1
            signal = mgr.check_devcontainer_session_drain("r1")
            if signal is not None:
                assert resumed_at is None, "resume must fire exactly once"
                resumed_at = drained
                assert signal is False

        assert resumed_at is not None, "drain-side check never resumed a stalled producer"
        assert queue.qsize() <= _DEVC_SESSION_QUEUE_LOW_WATER + 1

    asyncio.run(scenario())


def test_push_on_full_queue_drops_and_counts_never_awaits() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None
        # Fill to raw capacity (maxsize), not just the high-water mark.
        while not queue.full():
            mgr.push_devcontainer_session_chunk("r1", _b64(b"x"))
        # One more push must return immediately (no await anywhere in this
        # synchronous call), report no transition (already paused), and count.
        result = mgr.push_devcontainer_session_chunk("r1", _b64(b"overflow"))
        assert result is None
        assert mgr._devc_session_dropped_chunks["r1"] == 1

    asyncio.run(scenario())


def test_push_unknown_session_ref_is_a_silent_no_op() -> None:
    mgr = ConnectionManager()
    assert mgr.push_devcontainer_session_chunk("no-such-session", _b64(b"x")) is None


def test_push_malformed_base64_is_a_silent_no_op() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        assert mgr.push_devcontainer_session_chunk("r1", "not-valid-base64!!!") is None
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None and queue.qsize() == 0

    asyncio.run(scenario())


# ── idempotent unregister ─────────────────────────────────────────────────────


def test_unregister_is_idempotent_across_two_callers() -> None:
    """Mirrors the real interleaving: a session's own teardown and a disconnect
    reap can both call this for the same session_ref — neither may fault."""
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        mgr.unregister_devcontainer_session("s1", "r1")
        mgr.unregister_devcontainer_session("s1", "r1")  # must not raise
        assert mgr.get_devcontainer_session_queue("r1") is None
        assert "r1" not in mgr._client_pending_devc_sessions.get("s1", set())

    asyncio.run(scenario())


def test_unregister_wakes_a_consumer_blocked_on_queue_get() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None
        consumer = asyncio.ensure_future(queue.get())
        await asyncio.sleep(0)
        assert not consumer.done()
        mgr.unregister_devcontainer_session("s1", "r1")
        result = await asyncio.wait_for(consumer, timeout=1.0)
        assert result is None  # EOF sentinel

    asyncio.run(scenario())


def test_unregister_on_full_queue_still_delivers_eof_via_evict_then_put() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None
        while not queue.full():
            mgr.push_devcontainer_session_chunk("r1", _b64(b"x"))
        mgr.unregister_devcontainer_session("s1", "r1")
        # The EOF sentinel must be discoverable even though the queue was full
        # at unregister time — drain until we find it (evict-then-put may have
        # placed it anywhere behind whatever it evicted).
        found_eof = False
        while not queue.empty():
            if queue.get_nowait() is None:
                found_eof = True
                break
        assert found_eof

    asyncio.run(scenario())


# ── disconnect reap ───────────────────────────────────────────────────────────


def test_disconnect_reaps_open_sessions_and_wakes_the_open_waiter() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        task = asyncio.ensure_future(
            mgr.wait_devcontainer_session_opened(session_ref="r1", session_id="s1", timeout=5.0)
        )
        await asyncio.sleep(0)
        mgr._reap_client_state("s1")
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {"ok": False, "detail": "disconnected"}
        assert mgr.get_devcontainer_session_queue("r1") is None

    asyncio.run(scenario())


def test_disconnect_force_eofs_an_already_open_session() -> None:
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        queue = mgr.get_devcontainer_session_queue("r1")
        assert queue is not None
        mgr._reap_client_state("s1")
        assert await asyncio.wait_for(queue.get(), timeout=1.0) is None

    asyncio.run(scenario())


def test_disconnect_reap_and_session_teardown_interleave_without_faulting() -> None:
    """The exact race D1 calls out: a demux task's own `finally` and
    `_reap_client_state` both unregister the same session_ref."""
    mgr = ConnectionManager()

    async def scenario() -> None:
        await _open_session(mgr, "s1", "r1")
        mgr.unregister_devcontainer_session("s1", "r1")  # the "demux finally" side
        mgr._reap_client_state("s1")  # the "disconnect reap" side, same session_ref
        # Neither raised; state is consistently torn down either way.
        assert mgr.get_devcontainer_session_queue("r1") is None

    asyncio.run(scenario())
