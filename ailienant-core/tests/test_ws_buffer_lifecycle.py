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
from typing import Any, Dict

from api.websocket_manager import ConnectionManager


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
