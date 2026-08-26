"""DEBT-188 — a ``client_hitl_response`` must resume the session it answered,
not the WS connection it arrived on.

Root cause (found from a live user's own telemetry log): ``main.py`` resumed a
paused graph by ``client_id`` — the WS route's path parameter, stable for the
whole physical connection — while the paused graph is registered under the
chat's own ``session_id``. ``register_alias``/``RegisterSessionPayload``
already support several sessions sharing one connection; ``client_id`` and
``session_id`` only coincide when exactly one session is active on that
connection. Once a second session shares it (a retried prompt in a new
session, a second panel), the resume lookup silently misses, falls through to
the wrong HITL transport, and the paused graph is never resumed — no error,
no timeout, total silence, exactly as observed.
"""
from __future__ import annotations

import time

import pytest

from api.ws_contracts import HITLResponsePayload
from core.task_service import TaskPayload, TaskService
from main import _resolve_hitl_session_id

pytestmark = pytest.mark.anyio


def _payload() -> TaskPayload:
    return TaskPayload(task_prompt="q", dirty_buffers=[])


# ── _resolve_hitl_session_id — pure resolution logic ──────────────────────────


def test_resolves_to_the_reply_own_session_id_when_present() -> None:
    data = HITLResponsePayload(approval_id="a1", approved=True, session_id="c7cde11e")
    assert _resolve_hitl_session_id(data, client_id="e9d17a46") == "c7cde11e"


def test_falls_back_to_client_id_for_a_stale_webview_with_no_session_id() -> None:
    data = HITLResponsePayload(approval_id="a1", approved=True, session_id=None)
    assert _resolve_hitl_session_id(data, client_id="e9d17a46") == "e9d17a46"


# ── the actual defect class: two sessions sharing one connection ──────────────


async def test_two_sessions_sharing_one_connection_resume_independently() -> None:
    """Models the exact scenario from the reporting user's telemetry log: two
    chat sessions (a retried prompt in a fresh session) share one WS
    connection's client_id. Answering session B's clarification must resume
    B — and must NOT be satisfied by, or disturb, A's still-paused entry."""
    ts = TaskService()
    connection_client_id = "e9d17a46-ad64-414f-b057-44f98d3a4a6f"
    session_a = "58321ab3-e689-423f-8056-7c88a99ac5a7"
    session_b = "c7cde11e-1c78-4cf8-90d1-d7ab3ecd0024"

    ts._paused_tasks[session_a] = (_payload(), "SEQUENTIAL", time.monotonic())
    ts._paused_tasks[session_b] = (_payload(), "SEQUENTIAL", time.monotonic())

    reply_b = HITLResponsePayload(approval_id="approval-b", approved=True, session_id=session_b)
    resolved = _resolve_hitl_session_id(reply_b, client_id=connection_client_id)

    assert resolved == session_b
    assert ts.has_paused_graph(resolved) is True
    # A is untouched by resolving/answering B.
    assert ts.has_paused_graph(session_a) is True


async def test_pre_fix_behavior_would_have_missed_both_sessions() -> None:
    """Documents the actual failure mode: resuming by the bare connection id
    (the pre-fix behavior) finds neither session's pause, since a real
    connection id never equals either session's own id once more than one
    session shares the connection — reproducing the silent no-resume."""
    ts = TaskService()
    connection_client_id = "e9d17a46-ad64-414f-b057-44f98d3a4a6f"
    session_b = "c7cde11e-1c78-4cf8-90d1-d7ab3ecd0024"

    ts._paused_tasks[session_b] = (_payload(), "SEQUENTIAL", time.monotonic())

    assert ts.has_paused_graph(connection_client_id) is False
    assert ts.has_paused_graph(session_b) is True
