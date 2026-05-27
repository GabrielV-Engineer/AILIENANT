# tests/test_hitl_request_kind.py
"""Phase 7.11.7 DoD — Native HITL push-notification protocol (ADR-706 §4.5f).

Three tests covering the *additive* ``request_kind`` field added to
``HITLApprovalRequestPayload`` so the native VS Code toast can choose severity
(info vs warning) per request type. The frontend bridge is unit-tested
separately in ``ailienant-extension/src/test/hitlNotifier.test.ts``.

  1. Pydantic round-trip with ``request_kind=None`` — pre-7.11.7 wire shape
     still validates and dumps cleanly (backward-compat guarantee).
  2. Pydantic round-trip with ``request_kind="BUDGET_OVERFLOW"`` — the field
     survives JSON serialisation on the discriminated-union event wrapper.
  3. End-to-end emit — ``request_human_approval(..., request_kind="…")``
     threads the value into the ``ServerHITLApprovalRequestEvent`` payload
     that reaches ``send_personal_message``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.ws_contracts import (
    HITLApprovalRequestPayload,
    ServerHITLApprovalRequestEvent,
)
from api.websocket_manager import ConnectionManager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio backend to asyncio — matches every other async test file."""
    return "asyncio"


# ──────────────────────────────────────────────────────────────────────────────
# 1. Backward-compat: request_kind=None must round-trip cleanly
# ──────────────────────────────────────────────────────────────────────────────

def test_payload_round_trips_without_request_kind() -> None:
    """A payload that omits ``request_kind`` validates, dumps to JSON without
    the field appearing as a stray ``null`` mid-stream, and re-validates
    identically. This is the pre-7.11.7 wire shape; any old client that emits
    or consumes the payload must still parse it after the additive change."""
    payload = HITLApprovalRequestPayload(
        session_id="sess-A",
        approval_id="aid-1",
        action_description="Apply 1 file change(s): foo.py",
        proposed_content="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    assert payload.request_kind is None

    raw = payload.model_dump_json()
    restored = HITLApprovalRequestPayload.model_validate_json(raw)
    assert restored.session_id == "sess-A"
    assert restored.approval_id == "aid-1"
    assert restored.request_kind is None


# ──────────────────────────────────────────────────────────────────────────────
# 2. Forward: request_kind survives the discriminated-union round-trip
# ──────────────────────────────────────────────────────────────────────────────

def test_event_round_trips_with_populated_request_kind() -> None:
    """The full ``ServerHITLApprovalRequestEvent`` wrapper (event_type literal +
    nested data) must preserve ``request_kind`` through ``model_dump_json`` and
    back. Validates that the frontend WS-event switch will see the field."""
    event = ServerHITLApprovalRequestEvent(
        data=HITLApprovalRequestPayload(
            session_id="sess-B",
            approval_id="aid-2",
            action_description="BUDGET_OVERFLOW",
            proposed_content="cost=$1.2 budget=$1.0",
            request_kind="BUDGET_OVERFLOW",
        )
    )

    raw = event.model_dump_json()
    # `type(event).model_validate_json(raw)` returns the exact subclass; cast
    # to Any so mypy doesn't see only the BaseModel base in the round-trip var.
    restored: Any = ServerHITLApprovalRequestEvent.model_validate_json(raw)
    assert restored.event_type == "server_hitl_approval_request"
    assert restored.data.request_kind == "BUDGET_OVERFLOW"
    assert restored.data.approval_id == "aid-2"


# ──────────────────────────────────────────────────────────────────────────────
# 3. End-to-end: request_human_approval threads request_kind into the emit
# ──────────────────────────────────────────────────────────────────────────────

async def test_request_human_approval_threads_kind_into_emit() -> None:
    """Calling ``ConnectionManager.request_human_approval`` with a non-None
    ``request_kind`` must result in a ``ServerHITLApprovalRequestEvent`` whose
    payload carries that kind reaching ``send_personal_message``. We pin a
    tiny timeout so the wait_for() resolves to None (no client to answer),
    which is fine — the assertion is on the *emitted* event, not on the
    return value. We also patch ``log_audit_event`` to a no-op so the test
    never touches the audit DB.
    """
    manager = ConnectionManager()
    captured: dict[str, Any] = {}

    async def _capture(session_id: str, event: Any) -> None:
        captured["session_id"] = session_id
        captured["event"] = event

    # Patch the bound method on this fresh instance only.
    manager.send_personal_message = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    with patch("core.audit.log_audit_event", new=AsyncMock(return_value="dummyhash")):
        decision = await manager.request_human_approval(
            session_id="sess-C",
            action_description="DRIFT_DETECTED — similarity 0.42",
            proposed_content="Revised outcome: …",
            timeout_s=0.01,
            request_kind="DRIFT_DETECTED",
        )

    # No client answered within 10ms → decision is None (audited as "timeout").
    assert decision is None

    # The emitted event carries the request_kind verbatim.
    emitted_event = captured["event"]
    assert isinstance(emitted_event, ServerHITLApprovalRequestEvent)
    assert emitted_event.data.request_kind == "DRIFT_DETECTED"
    assert emitted_event.data.session_id == "sess-C"
    # Pending registry was cleaned up in the finally branch.
    assert emitted_event.data.approval_id not in manager._hitl_pending  # type: ignore[operator]
