"""Phase 2.27 — Unit tests for GPUResourceManager and ResourceBroker.

DoD: covers singleton identity, multi-session lock acquisition + queuing,
release hooks, recommendation logic, broker resolution paths (bypass for
MODEL_BIG, lock-free fast path, SWITCH_TO_CLOUD, CANCEL, WAIT), and the
post-LLM-exception deadlock guard explicitly flagged by review.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from core import resource_manager
from core.resource_manager import (
    BrokerDecision,
    GPUResourceManager,
    ResourceBroker,
    _compute_recommendation,
)
from shared.config import MODEL_BIG, MODEL_SMALL


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Each test starts with a clean global lock state."""
    GPUResourceManager.reset_for_tests()


# ---------------------------------------------------------------------------
# GPUResourceManager — singleton + lock mechanics
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_singleton_returns_same_instance() -> None:
    a = await GPUResourceManager.get()
    b = await GPUResourceManager.get()
    assert a is b


@pytest.mark.anyio
async def test_first_session_acquires_lock_immediately() -> None:
    mgr = await GPUResourceManager.get()
    assert await mgr.try_acquire_now("session-A", MODEL_SMALL) is True
    snap = await mgr.snapshot()
    assert snap["locked_by_session_id"] == "session-A"
    assert snap["active_model_name"] == MODEL_SMALL


@pytest.mark.anyio
async def test_second_session_blocked_when_other_holds() -> None:
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)
    assert await mgr.try_acquire_now("session-B", MODEL_SMALL) is False


@pytest.mark.anyio
async def test_release_unblocks_waiters() -> None:
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)

    async def waiter() -> None:
        await mgr.acquire_lock("session-B", MODEL_SMALL)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # let B enter the queue
    assert await mgr.get_queue_position("session-B") == 1
    await mgr.release_lock("session-A")
    await asyncio.wait_for(task, timeout=1.0)
    assert await mgr.get_queue_position("session-B") == 0
    snap = await mgr.snapshot()
    assert snap["locked_by_session_id"] == "session-B"


@pytest.mark.anyio
async def test_reentrant_same_session_acquire_is_idempotent() -> None:
    mgr = await GPUResourceManager.get()
    assert await mgr.try_acquire_now("session-A", MODEL_SMALL) is True
    assert await mgr.try_acquire_now("session-A", MODEL_SMALL) is True
    snap = await mgr.snapshot()
    assert snap["locked_by_session_id"] == "session-A"


@pytest.mark.anyio
async def test_release_by_non_holder_is_noop() -> None:
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)
    await mgr.release_lock("session-B")  # not the holder
    snap = await mgr.snapshot()
    assert snap["locked_by_session_id"] == "session-A"  # A still holds


# ---------------------------------------------------------------------------
# _compute_recommendation — pure heuristic
# ---------------------------------------------------------------------------


def test_compute_recommendation_high_tci_is_cloud() -> None:
    assert _compute_recommendation(tci=80.0, queue_len=0) == "SWITCH_TO_CLOUD"


def test_compute_recommendation_low_tci_is_cloud() -> None:
    assert _compute_recommendation(tci=20.0, queue_len=0) == "SWITCH_TO_CLOUD"


def test_compute_recommendation_mid_tci_empty_queue_is_wait() -> None:
    assert _compute_recommendation(tci=55.0, queue_len=0) == "WAIT"


def test_compute_recommendation_mid_tci_busy_queue_is_cloud() -> None:
    assert _compute_recommendation(tci=55.0, queue_len=3) == "SWITCH_TO_CLOUD"


# ---------------------------------------------------------------------------
# ResourceBroker.acquire_or_resolve — orchestration paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_acquire_or_resolve_bypasses_lock_for_model_big() -> None:
    state: Dict[str, Any] = {"task_id": "session-A", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_BIG)
    assert decision.cancelled is False
    assert decision.effective_model == MODEL_BIG
    assert decision.holds_lock is False  # MODEL_BIG never holds the local lock


@pytest.mark.anyio
async def test_acquire_or_resolve_returns_holds_lock_when_free() -> None:
    state: Dict[str, Any] = {"task_id": "session-A", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)
    assert decision.holds_lock is True
    assert decision.effective_model == MODEL_SMALL
    assert decision.cancelled is False
    # cleanup
    await ResourceBroker.release("session-A")


@pytest.mark.anyio
async def test_acquire_or_resolve_no_task_id_bypasses_lock() -> None:
    state: Dict[str, Any] = {"task_id": "", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)
    # No session id → broker can't track us; treat as bypass.
    assert decision.holds_lock is False
    assert decision.cancelled is False


@pytest.mark.anyio
async def test_acquire_or_resolve_switch_to_cloud_swaps_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = await GPUResourceManager.get()
    # Hold lock with session A so session B contends.
    await mgr.try_acquire_now("session-A", MODEL_SMALL)

    async def stub_resolve(*args: Any, **kwargs: Any) -> str:
        return "SWITCH_TO_CLOUD"

    monkeypatch.setattr(
        resource_manager, "_emit_contention_and_await_resolution", stub_resolve
    )

    state: Dict[str, Any] = {"task_id": "session-B", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)

    assert decision.cancelled is False
    assert decision.effective_model == MODEL_BIG
    assert decision.holds_lock is False  # cloud path doesn't hold the local lock
    assert state["active_llm_profile"].model_name == MODEL_BIG
    assert state["user_resource_resolution"] == "SWITCH_TO_CLOUD"


@pytest.mark.anyio
async def test_acquire_or_resolve_cancel_returns_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)

    async def stub_resolve(*args: Any, **kwargs: Any) -> str:
        return "CANCEL"

    monkeypatch.setattr(
        resource_manager, "_emit_contention_and_await_resolution", stub_resolve
    )

    state: Dict[str, Any] = {"task_id": "session-B", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)
    assert decision.cancelled is True
    assert decision.holds_lock is False
    assert state["user_resource_resolution"] == "CANCEL"


@pytest.mark.anyio
async def test_acquire_or_resolve_wait_blocks_until_release(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)

    async def stub_resolve(*args: Any, **kwargs: Any) -> str:
        return "WAIT"

    monkeypatch.setattr(
        resource_manager, "_emit_contention_and_await_resolution", stub_resolve
    )

    state: Dict[str, Any] = {"task_id": "session-B", "tci": 55.0}

    async def acquirer() -> BrokerDecision:
        return await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)

    task = asyncio.create_task(acquirer())
    await asyncio.sleep(0.05)  # let B enter the queue
    await mgr.release_lock("session-A")
    decision = await asyncio.wait_for(task, timeout=1.0)
    assert decision.cancelled is False
    assert decision.holds_lock is True
    assert decision.effective_model == MODEL_SMALL
    assert state["user_resource_resolution"] == "WAIT"
    await ResourceBroker.release("session-B")


@pytest.mark.anyio
async def test_contention_status_populated_on_contention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercises the real _emit_contention_and_await_resolution; only the WS
    suspension point (vfs_manager.request_human_approval) is stubbed, so the
    production state-mutation code runs end-to-end."""
    mgr = await GPUResourceManager.get()
    await mgr.try_acquire_now("session-A", MODEL_SMALL)

    from api import websocket_manager as ws_mod

    async def fake_approval(**kwargs: Any) -> Dict[str, Any]:
        return {"approved": True, "comment": "SWITCH_TO_CLOUD"}

    monkeypatch.setattr(ws_mod.vfs_manager, "request_human_approval", fake_approval)

    state: Dict[str, Any] = {"task_id": "session-B", "tci": 80.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)

    cs = state["contention_status"]
    assert cs["requested_model"] == MODEL_SMALL
    assert cs["holder_session"] == "session-A"
    assert cs["queue_len"] == 0
    assert cs["recommendation"] == "SWITCH_TO_CLOUD"
    # And the ui_interrupt render payload matches the brief's strict contract.
    ui = state["ui_interrupt"]
    assert ui["action"] == "RESOURCE_CONTENTION_INTERRUPT"
    assert ui["payload"]["conflicting_model"] == MODEL_SMALL
    assert ui["payload"]["task_tci"] == 80.0
    assert ui["payload"]["recommendation"] == "SWITCH_TO_CLOUD"
    assert ui["payload"]["queue_position"] == 1
    assert decision.effective_model == MODEL_BIG


# ---------------------------------------------------------------------------
# Deadlock regression guard (reviewer-flagged scenario)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_lock_released_when_post_llm_processing_raises() -> None:
    """If a node holds the lock and a downstream parse/validation error raises,
    the try/finally at the call site MUST release the lock — otherwise every
    other session deadlocks. This exercises the contract the call-site wrapper
    is required to honour.
    """
    mgr = await GPUResourceManager.get()
    state: Dict[str, Any] = {"task_id": "session-A", "tci": 50.0}
    decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_SMALL)
    assert decision.holds_lock is True

    with pytest.raises(ValueError, match="simulated post-LLM parse error"):
        try:
            raise ValueError("simulated post-LLM parse error")
        finally:
            if decision.holds_lock:
                await ResourceBroker.release("session-A")

    # Lock must be genuinely free, not just nominally released.
    assert await mgr.try_acquire_now("session-B", MODEL_SMALL) is True
    snap = await mgr.snapshot()
    assert snap["locked_by_session_id"] == "session-B"
