"""Inbound per-client WS token bucket (concurrency safety spine).

The Push model lets a save storm fire telemetry-class events faster than the
loop can absorb. ``ConnectionManager.allow_inbound`` sheds the excess per client
without touching interactive traffic, refills over time, isolates clients, and
forgets a client's throttle state on disconnect.
"""
from typing import Dict

import pytest

import api.websocket_manager as wm
from api.websocket_manager import ConnectionManager


def test_flood_is_shed_past_capacity() -> None:
    """A tight burst drains the bucket to its capacity; the rest is shed."""
    mgr = ConnectionManager()
    allowed = sum(1 for _ in range(150) if mgr.allow_inbound("c1"))
    # Capacity is 100; a microsecond-tight loop refills negligibly.
    assert 100 <= allowed <= 102
    assert mgr.allow_inbound("c1") is False


def test_bucket_refills_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens replenish at the refill rate as the monotonic clock advances."""
    mgr = ConnectionManager()
    clock: Dict[str, float] = {"t": 1000.0}
    monkeypatch.setattr(wm.time, "monotonic", lambda: clock["t"])

    for _ in range(int(wm._INBOUND_BUCKET_CAPACITY)):
        assert mgr.allow_inbound("c1") is True
    assert mgr.allow_inbound("c1") is False
    clock["t"] += 1.0  # +1s → + refill_rate tokens
    assert mgr.allow_inbound("c1") is True


def test_per_client_isolation_and_disconnect_purges() -> None:
    """Buckets are per-client; disconnect forgets the throttle state."""
    mgr = ConnectionManager()
    for _ in range(int(wm._INBOUND_BUCKET_CAPACITY)):
        mgr.allow_inbound("c1")
    assert mgr.allow_inbound("c1") is False
    assert mgr.allow_inbound("c2") is True   # a different client is unaffected
    mgr.disconnect("c1")
    assert mgr.allow_inbound("c1") is True    # reconnect starts full again
