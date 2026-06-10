"""Phase 7.19.6 DoD — interactive terminal stdin routing.

Verifies the bidirectional contract reaches the user's keystroke:
  - write_session_stdin feeds the exact bytes to the live session's write_stdin.
  - write_session_stdin returns False for an unknown session (no crash).
  - A failing session write is swallowed (best-effort) and returns False.
  - interrupt_session calls the session's interrupt().
  - A client_pty_write frame validates against the WebSocketMessage union.

Async cases use asyncio.run() — no pytest-asyncio dependency.
"""
from __future__ import annotations

import asyncio
from typing import List

import brain.agentic_cell as ac
from api.ws_contracts import ClientPtyWriteEvent
from api.websocket_manager import ws_adapter


class _StubSession:
    """Records stdin writes and interrupt calls; optionally fails the write."""

    def __init__(self, *, fail: bool = False) -> None:
        self.writes: List[bytes] = []
        self.interrupted = 0
        self._fail = fail

    async def write_stdin(self, data: bytes) -> None:
        if self._fail:
            raise RuntimeError("session torn down")
        self.writes.append(data)

    async def interrupt(self) -> None:
        self.interrupted += 1


def _register(session_id: str, session: _StubSession) -> None:
    ac._session_registry[session_id] = ac._CellSession(session=session, surface=None)


def setup_function(_func: object) -> None:
    ac._session_registry.clear()


def teardown_function(_func: object) -> None:
    ac._session_registry.clear()


def test_write_session_stdin_reaches_session() -> None:
    sess = _StubSession()
    _register("sess-a", sess)

    async def body() -> bool:
        return await ac.write_session_stdin("sess-a", b"yes\n")

    ok = asyncio.run(body())
    assert ok is True
    assert sess.writes == [b"yes\n"]


def test_write_session_stdin_unknown_session_is_false() -> None:
    async def body() -> bool:
        return await ac.write_session_stdin("nope", b"data\n")

    assert asyncio.run(body()) is False


def test_write_session_stdin_swallows_failure() -> None:
    sess = _StubSession(fail=True)
    _register("sess-fail", sess)

    async def body() -> bool:
        return await ac.write_session_stdin("sess-fail", b"x\n")

    # Best-effort: a dead session must not raise; it reports False.
    assert asyncio.run(body()) is False


def test_interrupt_session_signals() -> None:
    sess = _StubSession()
    _register("sess-int", sess)

    async def body() -> bool:
        return await ac.interrupt_session("sess-int")

    assert asyncio.run(body()) is True
    assert sess.interrupted == 1


def test_interrupt_unknown_session_is_false() -> None:
    async def body() -> bool:
        return await ac.interrupt_session("ghost")

    assert asyncio.run(body()) is False


def test_client_pty_write_validates_in_union() -> None:
    frame = {
        "event_type": "client_pty_write",
        "data": {"session_id": "sess-a", "data": "y\n"},
    }
    event = ws_adapter.validate_python(frame)
    assert isinstance(event, ClientPtyWriteEvent)
    assert event.data.session_id == "sess-a"
    assert event.data.data == "y\n"
