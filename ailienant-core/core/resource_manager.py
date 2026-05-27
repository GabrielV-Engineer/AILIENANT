# ailienant-core/core/resource_manager.py
"""Phase 2.27 — Global Resource Broker for cross-session VRAM contention.

`GPUResourceManager` is a process-wide async singleton that serialises local
LLM invocations across concurrent AILIENANT sessions. `ResourceBroker` wraps
the gateway path: on contention it pauses the calling node via the project's
established HitL convention (vfs_manager.request_human_approval — same as
brain/drift_monitor.py and brain/finops.py) and resumes along the user's
chosen branch (WAIT, SWITCH_TO_CLOUD, CANCEL).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger("AILIENANT_RESOURCE_BROKER")

Resolution = Literal["WAIT", "SWITCH_TO_CLOUD", "CANCEL"]
_DEFAULT_HITL_TIMEOUT_S: float = 300.0
_ETA_PER_QUEUE_POSITION_S: int = 20


@dataclass
class _LockState:
    active_model_name: Optional[str] = None
    locked_by_session_id: Optional[str] = None
    lock_timestamp: float = 0.0
    queue: List[str] = field(default_factory=list)


class GPUResourceManager:
    """Process-wide async singleton enforcing single-holder local-model VRAM lock."""

    _instance: Optional["GPUResourceManager"] = None
    _instance_lock: Optional[asyncio.Lock] = None

    def __init__(self) -> None:
        self._state = _LockState()
        self._mutex: asyncio.Lock = asyncio.Lock()
        self._release_event: asyncio.Event = asyncio.Event()
        self._release_event.set()  # initially free

    @classmethod
    async def get(cls) -> "GPUResourceManager":
        if cls._instance_lock is None:
            cls._instance_lock = asyncio.Lock()
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Test-only: drops the singleton so each test starts with a clean lock."""
        cls._instance = None
        cls._instance_lock = None

    async def try_acquire_now(self, session_id: str, model_name: str) -> bool:
        """Non-blocking acquire. True if lock taken; False if held by another session."""
        async with self._mutex:
            if self._state.locked_by_session_id is None:
                self._state.active_model_name = model_name
                self._state.locked_by_session_id = session_id
                self._state.lock_timestamp = time.monotonic()
                self._release_event.clear()
                return True
            if self._state.locked_by_session_id == session_id:
                self._state.active_model_name = model_name
                return True
            return False

    async def acquire_lock(self, session_id: str, model_name: str) -> None:
        """Blocking acquire — joins the queue and waits until the lock is free."""
        async with self._mutex:
            if (
                session_id not in self._state.queue
                and self._state.locked_by_session_id != session_id
            ):
                self._state.queue.append(session_id)
        while True:
            if await self.try_acquire_now(session_id, model_name):
                async with self._mutex:
                    if session_id in self._state.queue:
                        self._state.queue.remove(session_id)
                return
            await self._release_event.wait()

    async def release_lock(self, session_id: str) -> None:
        """Release the lock only if the caller is the current holder."""
        async with self._mutex:
            if self._state.locked_by_session_id != session_id:
                return
            self._state.active_model_name = None
            self._state.locked_by_session_id = None
            self._state.lock_timestamp = 0.0
            self._release_event.set()

    async def get_queue_position(self, session_id: str) -> int:
        """1-based queue position; 0 if currently holding the lock or not queued."""
        async with self._mutex:
            if self._state.locked_by_session_id == session_id:
                return 0
            try:
                return self._state.queue.index(session_id) + 1
            except ValueError:
                return 0

    async def snapshot(self) -> Dict[str, Any]:
        """Atomic snapshot for telemetry payloads."""
        async with self._mutex:
            return {
                "active_model_name": self._state.active_model_name,
                "locked_by_session_id": self._state.locked_by_session_id,
                "lock_timestamp": self._state.lock_timestamp,
                "queue": list(self._state.queue),
                "queue_len": len(self._state.queue),
            }


@dataclass
class BrokerDecision:
    """Result of a pre-flight resource check.

    If `holds_lock` is True the caller MUST wrap subsequent work in a try/finally
    that calls `ResourceBroker.release(session_id)` — even on parse/validation
    errors after the LLM returns. Failing to release would deadlock every other
    session waiting on local VRAM.
    """

    cancelled: bool
    effective_model: str
    holds_lock: bool
    contention_status: Optional[Dict[str, Any]] = None


def _compute_recommendation(tci: float, queue_len: int) -> Resolution:
    """Brief §3b — recommendation heuristic.

    - TCI > 75               → SWITCH_TO_CLOUD (heavy task, cloud is faster)
    - TCI < 40               → SWITCH_TO_CLOUD (light task, don't make user wait)
    - mid + empty queue (=0) → WAIT             (we're next; cheap to wait)
    - mid + busy queue       → SWITCH_TO_CLOUD
    """
    if tci > 75.0:
        return "SWITCH_TO_CLOUD"
    if tci < 40.0:
        return "SWITCH_TO_CLOUD"
    return "WAIT" if queue_len == 0 else "SWITCH_TO_CLOUD"


async def _emit_contention_and_await_resolution(
    state: Dict[str, Any],
    snapshot: Dict[str, Any],
    requested_model: str,
    recommendation: Resolution,
    queue_position: int,
) -> Resolution:
    """Build the brief's ui_interrupt payload, suspend via request_human_approval,
    parse the user's reply. Deferred import avoids websocket→core circular deps.

    The rich payload is also packed into HITLApprovalRequestPayload.proposed_content
    as JSON so the extension can render the modal without ws_contracts changes.
    The sentinel action_description="RESOURCE_CONTENTION" lets the frontend
    discriminate this from regular approval prompts.
    """
    from api.websocket_manager import vfs_manager

    payload: Dict[str, Any] = {
        "action": "RESOURCE_CONTENTION_INTERRUPT",
        "payload": {
            "conflicting_model": snapshot.get("active_model_name") or requested_model,
            "task_tci": float(state.get("tci", 0.0)),
            "recommendation": recommendation,
            "queue_position": queue_position,
            "estimated_wait_seconds": queue_position * _ETA_PER_QUEUE_POSITION_S,
        },
    }
    state["ui_interrupt"] = payload
    state["contention_status"] = {
        "requested_model": requested_model,
        "holder_session": snapshot.get("locked_by_session_id"),
        "queue_len": snapshot.get("queue_len", 0),
        "recommendation": recommendation,
    }

    session_id = str(state.get("task_id", ""))
    if not session_id:
        logger.warning(
            "Resource contention without task_id; defaulting to SWITCH_TO_CLOUD."
        )
        return "SWITCH_TO_CLOUD"

    response = await vfs_manager.request_human_approval(
        session_id=session_id,
        action_description="RESOURCE_CONTENTION",
        proposed_content=json.dumps(payload),
        timeout_s=_DEFAULT_HITL_TIMEOUT_S,
        request_kind="RESOURCE_CONTENTION",
    )
    if response is None:
        logger.warning(
            "Resource contention HitL timed out for session=%s; defaulting to SWITCH_TO_CLOUD.",
            session_id,
        )
        return "SWITCH_TO_CLOUD"

    raw = (response.get("comment") or "").strip().upper()
    if raw in {"WAIT", "SWITCH_TO_CLOUD", "CANCEL"}:
        return raw  # type: ignore[return-value]
    logger.warning(
        "Resource contention: unrecognised resolution %r for session=%s; defaulting to SWITCH_TO_CLOUD.",
        raw,
        session_id,
    )
    return "SWITCH_TO_CLOUD"


class ResourceBroker:
    """Thin orchestrator wrapping LLMGateway calls with VRAM contention handling."""

    @staticmethod
    async def acquire_or_resolve(state: Dict[str, Any], model: str) -> BrokerDecision:
        """Pre-flight check. Returns a BrokerDecision describing:
          - which model to actually invoke (possibly substituted to MODEL_BIG)
          - whether the caller must release_lock() after the call
          - whether the user cancelled
        """
        from shared.config import MODEL_BIG  # deferred — avoids import cycle

        # MODEL_BIG / cloud calls bypass the lock entirely.
        if model == MODEL_BIG:
            return BrokerDecision(cancelled=False, effective_model=model, holds_lock=False)

        session_id = str(state.get("task_id", ""))
        if not session_id:
            return BrokerDecision(cancelled=False, effective_model=model, holds_lock=False)

        mgr = await GPUResourceManager.get()
        if await mgr.try_acquire_now(session_id, model):
            return BrokerDecision(cancelled=False, effective_model=model, holds_lock=True)

        snapshot = await mgr.snapshot()
        recommendation = _compute_recommendation(
            tci=float(state.get("tci", 0.0)),
            queue_len=int(snapshot.get("queue_len", 0)),
        )
        queue_position = int(snapshot.get("queue_len", 0)) + 1
        resolution = await _emit_contention_and_await_resolution(
            state=state,
            snapshot=snapshot,
            requested_model=model,
            recommendation=recommendation,
            queue_position=queue_position,
        )
        state["user_resource_resolution"] = resolution

        if resolution == "CANCEL":
            return BrokerDecision(
                cancelled=True,
                effective_model=model,
                holds_lock=False,
                contention_status=state.get("contention_status"),
            )
        if resolution == "SWITCH_TO_CLOUD":
            from brain.state import LLMProfile
            state["active_llm_profile"] = LLMProfile(
                model_name=MODEL_BIG,
                parameters_b=0.0,
                context_window=200_000,
                quantization="cloud",
            )
            return BrokerDecision(
                cancelled=False,
                effective_model=MODEL_BIG,
                holds_lock=False,
                contention_status=state.get("contention_status"),
            )
        # WAIT — block until the lock is ours.
        await mgr.acquire_lock(session_id, model)
        return BrokerDecision(
            cancelled=False,
            effective_model=model,
            holds_lock=True,
            contention_status=state.get("contention_status"),
        )

    @staticmethod
    async def release(session_id: str) -> None:
        """Release the lock for the given session. No-op if session_id is empty
        or if another session currently holds the lock."""
        if not session_id:
            return
        mgr = await GPUResourceManager.get()
        await mgr.release_lock(session_id)
