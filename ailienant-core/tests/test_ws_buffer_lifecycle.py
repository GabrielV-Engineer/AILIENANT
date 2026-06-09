"""WebSocket request-buffer lifecycle (memory-leak regression).

``ConnectionManager`` suspends a coroutine on an ``asyncio.Event`` keyed by a
one-shot UUID and stores the inbound result in a sibling dict. Two failure modes
used to strand entries in those dicts:

  1. A late-arriving ack/response (after the waiter timed out or was cancelled)
     was stored unconditionally, with no consumer left to pop it.
  2. A client disconnecting mid-request left its suspended waiter's buffers in
     place — and the waiter itself parked until its full timeout elapsed.

These tests pin both behaviours: late results are dropped at the store, and a
disconnect reaps the buffers while waking any live waiter so it returns at once.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from api.websocket_manager import ConnectionManager


class _FakeWS:
    """Minimal stand-in for a Starlette WebSocket — captures sent frames."""

    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


# ──────────────────────────────────────────────────────────────────────────────
# 1–2. Guard-at-store: a late result with no registered waiter is dropped.
# ──────────────────────────────────────────────────────────────────────────────

def test_late_hitl_response_is_dropped() -> None:
    """resolve_human_approval for an unknown approval_id must not buffer."""
    mgr = ConnectionManager()
    mgr.resolve_human_approval("dead-approval", approved=True, comment="late")
    assert mgr._hitl_responses == {}


def test_late_patch_ack_is_dropped() -> None:
    """resolve_patch_ack for an unknown patch_id must not buffer."""
    mgr = ConnectionManager()
    mgr.resolve_patch_ack("dead-patch", {"ok": True})
    assert mgr._patch_ack_results == {}


# ──────────────────────────────────────────────────────────────────────────────
# 3–4. Disconnect sweeps the buffers of an in-flight waiter for that client.
# ──────────────────────────────────────────────────────────────────────────────

def test_disconnect_sweeps_inflight_hitl() -> None:
    """A mid-request disconnect reaps the HITL event + response + index."""
    mgr = ConnectionManager()
    approval_id = "a1"
    mgr._client_pending_hitl["c1"] = {approval_id}
    mgr._hitl_pending[approval_id] = asyncio.Event()
    mgr._hitl_responses[approval_id] = {"approved": True}

    mgr.disconnect("c1")

    assert mgr._client_pending_hitl == {}
    assert mgr._hitl_pending == {}
    assert mgr._hitl_responses == {}


def test_disconnect_sweeps_inflight_patch_acks() -> None:
    """A mid-request disconnect reaps the patch event + result + index."""
    mgr = ConnectionManager()
    patch_id = "p1"
    mgr._client_pending_acks["c1"] = {patch_id}
    mgr._patch_acks[patch_id] = asyncio.Event()
    mgr._patch_ack_results[patch_id] = {"ok": True}

    mgr.disconnect("c1")

    assert mgr._client_pending_acks == {}
    assert mgr._patch_acks == {}
    assert mgr._patch_ack_results == {}


# ──────────────────────────────────────────────────────────────────────────────
# 5. End-to-end race: a waiter times out, then a late ack lands → no orphan.
# ──────────────────────────────────────────────────────────────────────────────

def test_timed_out_waiter_then_late_ack_leaves_no_orphan() -> None:
    """wait_patch_ack times out and clears its index; a late ack is then dropped."""
    async def _scenario() -> None:
        mgr = ConnectionManager()
        result = await mgr.wait_patch_ack("p2", "c1", timeout=0.01)
        assert result is None
        # Waiter is gone; its index entry was pruned in the finally block.
        assert mgr._client_pending_acks == {}
        assert mgr._patch_acks == {}
        # A straggling ack now has no waiter and must be dropped, not buffered.
        mgr.resolve_patch_ack("p2", {"ok": True})
        assert mgr._patch_ack_results == {}

    asyncio.run(_scenario())


# ──────────────────────────────────────────────────────────────────────────────
# 6. Disconnect wakes a live waiter — it returns None promptly (no zombie task).
# ──────────────────────────────────────────────────────────────────────────────

def test_disconnect_wakes_live_patch_waiter() -> None:
    """A suspended wait_patch_ack returns at disconnect, well before its timeout."""
    async def _scenario() -> None:
        mgr = ConnectionManager()
        task: "asyncio.Task[Dict[str, Any] | None]" = asyncio.ensure_future(
            mgr.wait_patch_ack("p3", "c1", timeout=30.0)
        )
        # Yield so the waiter reaches its suspension point and registers itself.
        await asyncio.sleep(0)
        assert mgr._patch_acks.get("p3") is not None

        mgr.disconnect("c1")

        # The waiter must complete almost immediately — not after 30s.
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None
        assert mgr._patch_acks == {}
        assert mgr._patch_ack_results == {}
        assert mgr._client_pending_acks == {}

    asyncio.run(_scenario())


# ──────────────────────────────────────────────────────────────────────────────
# 7–10. Multiplexing: one socket serves many sessions via register_alias, and
# every outbound event is tagged with the session id it is routed to.
# ──────────────────────────────────────────────────────────────────────────────

def test_register_alias_routes_session_to_connection_socket() -> None:
    """A session aliased onto a connection resolves to that connection's socket."""
    mgr = ConnectionManager()
    sock = _FakeWS()
    mgr.active_connections["conn"] = sock  # type: ignore[assignment]
    mgr.register_alias("sessA", "conn")
    assert mgr.active_connections["sessA"] is sock
    assert mgr._aliases["conn"] == {"sessA"}


def test_disconnect_reaps_all_aliases_and_their_buffers() -> None:
    """Closing the socket evicts the connection id AND every aliased session."""
    mgr = ConnectionManager()
    sock = _FakeWS()
    mgr.active_connections["conn"] = sock  # type: ignore[assignment]
    mgr.register_alias("sessA", "conn")
    mgr.register_alias("sessB", "conn")
    # An in-flight HITL on an aliased session must be reaped too.
    mgr._client_pending_hitl["sessA"] = {"ap1"}
    mgr._hitl_pending["ap1"] = asyncio.Event()

    mgr.disconnect("conn")

    assert "conn" not in mgr.active_connections
    assert "sessA" not in mgr.active_connections
    assert "sessB" not in mgr.active_connections
    assert mgr._aliases == {}
    assert mgr._client_pending_hitl == {}
    assert mgr._hitl_pending == {}


def test_stale_close_after_reconnect_is_noop() -> None:
    """A reconnect re-points the id to a fresh socket; the dead socket's close
    must not tear down the live connection or its aliases."""
    mgr = ConnectionManager()
    old, new = _FakeWS(), _FakeWS()
    mgr.active_connections["conn"] = old  # type: ignore[assignment]
    mgr.register_alias("sessA", "conn")
    # Reconnect under the same id: a new socket takes over and re-announces.
    mgr.active_connections["conn"] = new  # type: ignore[assignment]
    mgr.register_alias("sessA", "conn")

    # The OLD socket's belated close fires — identity guard must make it a no-op.
    mgr.disconnect("conn", old)  # type: ignore[arg-type]

    assert mgr.active_connections.get("conn") is new
    assert mgr.active_connections.get("sessA") is new


def test_send_personal_message_stamps_session_id() -> None:
    """The egress chokepoint injects data.session_id so the client can demux."""
    async def _scenario() -> None:
        from api.ws_contracts import ServerTokenChunkEvent, TokenChunkPayload
        mgr = ConnectionManager()
        sock = _FakeWS()
        mgr.active_connections["sessZ"] = sock  # type: ignore[assignment]

        await mgr.send_personal_message(
            "sessZ", ServerTokenChunkEvent(data=TokenChunkPayload(token="hi"))
        )

        assert len(sock.sent) == 1
        obj = json.loads(sock.sent[0])
        assert obj["event_type"] == "server_token_chunk"
        assert obj["data"]["session_id"] == "sessZ"   # stamped — was not on the model
        assert obj["data"]["token"] == "hi"

    asyncio.run(_scenario())


def test_send_personal_message_preserves_existing_session_id() -> None:
    """An event whose payload already names a session is not clobbered (setdefault)."""
    async def _scenario() -> None:
        from api.ws_contracts import (
            ServerHITLApprovalRequestEvent,
            HITLApprovalRequestPayload,
        )
        mgr = ConnectionManager()
        sock = _FakeWS()
        mgr.active_connections["connX"] = sock  # type: ignore[assignment]

        evt = ServerHITLApprovalRequestEvent(
            data=HITLApprovalRequestPayload(
                session_id="real-session",
                approval_id="ap",
                action_description="x",
            )
        )
        await mgr.send_personal_message("connX", evt)

        obj = json.loads(sock.sent[0])
        assert obj["data"]["session_id"] == "real-session"

    asyncio.run(_scenario())
