"""Devcontainer host execution-bridge WS contract + transport primitives.

The trusted devcontainer tier routes provisioning and command execution over an
additive WS contract: the backend ``ConnectionManager`` suspends a coroutine on a
per-``request_id`` ``asyncio.Event`` and the host streams status/output/exit
frames back. These tests pin the transport primitives in isolation (the receive
loop and the concrete bridge are wired separately):

  * the provision round-trip resolves only on a terminal status,
  * an interim ``provisioning`` tick must NOT wake the provision waiter,
  * the exec round-trip aggregates streamed chunks until the exit frame,
  * a timeout leaves no orphaned buffers,
  * a disconnect reaps any in-flight waiter so it returns at once (never hangs),
  * each new event round-trips through the inbound discriminated-union adapter
    and tolerates an unknown extra field (additive contract).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from api.websocket_manager import ConnectionManager, ws_adapter


class _FakeWS:
    """Minimal stand-in for a Starlette WebSocket — captures sent frames."""

    def __init__(self) -> None:
        self.sent: List[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


def _mgr_with_session(session_id: str) -> "tuple[ConnectionManager, _FakeWS]":
    mgr = ConnectionManager()
    sock = _FakeWS()
    mgr.active_connections[session_id] = sock  # type: ignore[assignment]
    return mgr, sock


# ──────────────────────────────────────────────────────────────────────────────
# 1. Exec round-trip: chunks aggregate, exit resolves the waiter.
# ──────────────────────────────────────────────────────────────────────────────

def test_exec_round_trip_aggregates_streams_and_exit() -> None:
    async def _scenario() -> None:
        mgr, sock = _mgr_with_session("sessE")

        async def _driver() -> None:
            # Yield so the waiter registers its buffers before chunks land.
            await asyncio.sleep(0)
            await mgr.emit_devcontainer_exec_request(
                "sessE", "req1", command="pytest -q", cwd="/work", env_keys=["CI"]
            )
            mgr.append_devcontainer_stream("req1", "stdout", "2 passed")
            mgr.append_devcontainer_stream("req1", "stdout", " in 0.1s")
            mgr.append_devcontainer_stream("req1", "stderr", "warning: x")
            mgr.resolve_devcontainer_exit("req1", exit_code=0)

        waiter = asyncio.ensure_future(
            mgr.wait_devcontainer_exec("req1", "sessE", timeout=5.0)
        )
        await asyncio.gather(waiter, _driver())
        result: Optional[Dict[str, Any]] = waiter.result()

        assert result is not None
        assert result["stdout"] == "2 passed in 0.1s"
        assert result["stderr"] == "warning: x"
        assert result["exit_code"] == 0

        # The dispatched request frame names the env KEYS only (never values).
        obj = json.loads(sock.sent[0])
        assert obj["event_type"] == "server_devcontainer_exec_request"
        assert obj["data"]["env_keys"] == ["CI"]
        assert "env" not in obj["data"]

        # No orphaned buffers after resolution.
        assert mgr._devc_exec_events == {}
        assert mgr._devc_exec_buffers == {}
        assert mgr._devc_exec_exit == {}
        assert mgr._client_pending_devc == {}

    asyncio.run(_scenario())


# ──────────────────────────────────────────────────────────────────────────────
# 2. Provision round-trip: terminal status resolves; interim tick does not.
# ──────────────────────────────────────────────────────────────────────────────

def test_provision_round_trip_resolves_on_ready() -> None:
    async def _scenario() -> None:
        mgr, _ = _mgr_with_session("sessP")

        async def _driver() -> None:
            await asyncio.sleep(0)
            mgr.resolve_devcontainer_provision("req2", "ready")

        waiter = asyncio.ensure_future(
            mgr.wait_devcontainer_provision("req2", "sessP", timeout=5.0)
        )
        await asyncio.gather(waiter, _driver())

        assert waiter.result() == "ready"
        assert mgr._devc_provision_events == {}
        assert mgr._devc_provision_state == {}
        assert mgr._client_pending_devc == {}

    asyncio.run(_scenario())


def test_interim_provisioning_tick_does_not_wake_waiter() -> None:
    async def _scenario() -> None:
        mgr, _ = _mgr_with_session("sessP")

        waiter = asyncio.ensure_future(
            mgr.wait_devcontainer_provision("req3", "sessP", timeout=30.0)
        )
        await asyncio.sleep(0)
        assert mgr._devc_provision_events.get("req3") is not None

        # An interim tick is progress telemetry — the waiter must stay parked.
        mgr.resolve_devcontainer_provision("req3", "provisioning")
        await asyncio.sleep(0)
        assert not waiter.done()

        # A terminal state then resolves it.
        mgr.resolve_devcontainer_provision("req3", "ready")
        assert await asyncio.wait_for(waiter, timeout=1.0) == "ready"

    asyncio.run(_scenario())


# ──────────────────────────────────────────────────────────────────────────────
# 3. Timeout leaves no orphan; unknown-id resolves are dropped.
# ──────────────────────────────────────────────────────────────────────────────

def test_exec_timeout_leaves_no_orphan() -> None:
    async def _scenario() -> None:
        mgr, _ = _mgr_with_session("sessT")
        result = await mgr.wait_devcontainer_exec("reqT", "sessT", timeout=0.01)
        assert result is None
        assert mgr._devc_exec_events == {}
        assert mgr._devc_exec_buffers == {}
        assert mgr._devc_exec_exit == {}
        assert mgr._client_pending_devc == {}
        # A straggling exit now has no waiter and must be dropped, not buffered.
        mgr.resolve_devcontainer_exit("reqT", exit_code=0)
        assert mgr._devc_exec_exit == {}

    asyncio.run(_scenario())


def test_late_provision_status_for_unknown_request_is_dropped() -> None:
    mgr = ConnectionManager()
    mgr.resolve_devcontainer_provision("dead-req", "ready")
    assert mgr._devc_provision_state == {}


# ──────────────────────────────────────────────────────────────────────────────
# 4. Disconnect wakes any in-flight devcontainer waiter → returns None.
# ──────────────────────────────────────────────────────────────────────────────

def test_disconnect_wakes_live_exec_waiter() -> None:
    async def _scenario() -> None:
        mgr, _ = _mgr_with_session("sessD")
        task: "asyncio.Task[Dict[str, Any] | None]" = asyncio.ensure_future(
            mgr.wait_devcontainer_exec("reqD", "sessD", timeout=30.0)
        )
        await asyncio.sleep(0)
        assert mgr._devc_exec_events.get("reqD") is not None

        mgr.disconnect("sessD")

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None
        assert mgr._devc_exec_events == {}
        assert mgr._devc_exec_buffers == {}
        assert mgr._devc_exec_exit == {}
        assert mgr._client_pending_devc == {}

    asyncio.run(_scenario())


# ──────────────────────────────────────────────────────────────────────────────
# 5. Inbound discriminated-union validation tolerates an unknown extra field.
# ──────────────────────────────────────────────────────────────────────────────

def test_inbound_events_validate_and_tolerate_unknown_fields() -> None:
    exec_stream = ws_adapter.validate_json(json.dumps({
        "event_type": "client_devcontainer_exec_stream",
        "data": {
            "session_id": "s", "request_id": "r",
            "stream": "stdout", "chunk": "hello",
            "future_field": 123,   # additive: unknown field tolerated
        },
    }))
    assert exec_stream.event_type == "client_devcontainer_exec_stream"
    assert exec_stream.data.chunk == "hello"

    exec_exit = ws_adapter.validate_json(json.dumps({
        "event_type": "client_devcontainer_exec_exit",
        "data": {"session_id": "s", "request_id": "r", "exit_code": 0},
    }))
    assert exec_exit.event_type == "client_devcontainer_exec_exit"
    assert exec_exit.data.exit_code == 0

    status = ws_adapter.validate_json(json.dumps({
        "event_type": "client_devcontainer_provision_status",
        "data": {"session_id": "s", "request_id": "r", "state": "ready"},
    }))
    assert status.event_type == "client_devcontainer_provision_status"
    assert status.data.state == "ready"
