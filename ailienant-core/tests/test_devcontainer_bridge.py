"""WebSocketHostBridge — the concrete trusted-tier host bridge.

The bridge maps the `HostExecutionBridge` Protocol onto the `ConnectionManager`
transport primitives: it mints a `request_id`/`session_ref`, emits the
server→host request, and awaits the host reply. The one-shot exec suite below
drives it with a **fake manager** (no real socket) to prove the round-trips and
the timeout/degrade mapping in isolation.

The interactive-session suite (§43, DEBT-084) drives it against a **real**
`ConnectionManager` instead: `_BridgeSandboxSession`'s demux task consumes the
manager's own bounded queue and backpressure state machine, so a hand-rolled
fake would just re-implement (and re-test) what `test_devcontainer_session_manager.py`
already covers directly. Only the socket itself and the extension-host process
are out of scope here — the host's replies are simulated by calling the same
`resolve_*`/`push_*` methods `main.py`'s receive loop calls on a real inbound
frame.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from typing import Any, Dict, List, Optional, Tuple

import pytest

from api.devcontainer_bridge import WebSocketHostBridge
from api.websocket_manager import ConnectionManager
from core.pty_session import SandboxSession, SandboxSessionError


class _FakeManager:
    """Records emitted requests and returns scripted waiter results."""

    def __init__(
        self,
        *,
        provision_state: Optional[str] = "ready",
        exec_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._provision_state = provision_state
        self._exec_result = exec_result
        self.provision_requests: List[Dict[str, Any]] = []
        self.exec_requests: List[Dict[str, Any]] = []

    async def emit_devcontainer_provision_request(
        self, *, session_id: str, request_id: str, cwd: str
    ) -> None:
        self.provision_requests.append(
            {"session_id": session_id, "request_id": request_id, "cwd": cwd}
        )

    async def wait_devcontainer_provision(
        self, *, request_id: str, session_id: str, timeout: float
    ) -> Optional[str]:
        return self._provision_state

    async def emit_devcontainer_exec_request(
        self, *, session_id: str, request_id: str, command: str, cwd: str,
        env_keys: List[str],
    ) -> None:
        self.exec_requests.append({
            "session_id": session_id, "request_id": request_id,
            "command": command, "cwd": cwd, "env_keys": env_keys,
        })

    async def wait_devcontainer_exec(
        self, *, request_id: str, session_id: str, timeout: float
    ) -> Optional[Dict[str, Any]]:
        return self._exec_result


def test_ensure_provisioned_true_on_ready() -> None:
    mgr = _FakeManager(provision_state="ready")
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]
    ok = asyncio.run(bridge.ensure_provisioned(session_id="s", cwd="/work"))
    assert ok is True
    assert mgr.provision_requests[0]["session_id"] == "s"


def test_ensure_provisioned_false_on_non_ready() -> None:
    for state in ("failed", "timeout", None):
        mgr = _FakeManager(provision_state=state)
        bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]
        ok = asyncio.run(bridge.ensure_provisioned(session_id="s", cwd="/work"))
        assert ok is False, f"state={state!r} should not be ready"


def test_exec_command_maps_result_and_sends_env_keys_only() -> None:
    mgr = _FakeManager(exec_result={"stdout": "hi", "stderr": "warn", "exit_code": 0})
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]
    result = asyncio.run(bridge.exec_command(
        session_id="s", command="pytest -q", cwd="/work",
        env_whitelist={"CI": "1", "PYTHONPATH": "/x"}, timeout_s=5.0,
    ))
    assert result.exit_code == 0
    assert result.stdout == "hi"
    assert result.stderr == "warn"
    # Names only on the wire — never the values.
    sent = mgr.exec_requests[0]
    assert sorted(sent["env_keys"]) == ["CI", "PYTHONPATH"]
    assert "1" not in sent["env_keys"] and "/x" not in sent["env_keys"]


def test_exec_command_degrades_on_no_reply() -> None:
    mgr = _FakeManager(exec_result=None)  # timeout / disconnect
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]
    result = asyncio.run(bridge.exec_command(
        session_id="s", command="sleep 99", cwd="/work",
        env_whitelist={}, timeout_s=0.1,
    ))
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_exec_no_reply]"


# =====================================================================
# open_host_session — failure paths (fake manager: no queue machinery needed)
# =====================================================================


class _OpenScriptedManager(_FakeManager):
    def __init__(self, *, open_result: Optional[Dict[str, Any]]) -> None:
        super().__init__()
        self._open_result = open_result

    async def emit_devcontainer_session_open(self, **_: Any) -> None:
        pass

    async def wait_devcontainer_session_opened(self, **_: Any) -> Optional[Dict[str, Any]]:
        return self._open_result


def test_open_host_session_raises_on_explicit_failure() -> None:
    mgr = _OpenScriptedManager(open_result={"ok": False, "detail": "no devcontainer.json"})
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(SandboxSessionError, match="no devcontainer.json"):
            await bridge.open_host_session(
                session_id="s", cwd="/work", env_whitelist={}, pre_spawn_guard=None,
            )

    asyncio.run(scenario())


def test_open_host_session_raises_on_timeout() -> None:
    mgr = _OpenScriptedManager(open_result=None)  # timeout / disconnect
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(SandboxSessionError):
            await bridge.open_host_session(
                session_id="s", cwd="/work", env_whitelist={}, pre_spawn_guard=None,
            )

    asyncio.run(scenario())


# =====================================================================
# Interactive session bridge (§43, DEBT-084) — real ConnectionManager
# =====================================================================


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _open_bridge_session(
    mgr: ConnectionManager, bridge: WebSocketHostBridge, session_id: str,
) -> Tuple[SandboxSession, str]:
    """Drive `open_host_session` to completion against a real ConnectionManager,
    standing in for the host's open-confirmation reply (no real socket)."""
    captured: "asyncio.Queue[str]" = asyncio.Queue()
    real_emit_open = mgr.emit_devcontainer_session_open

    async def _capturing_emit_open(
        session_id: str, session_ref: str, cwd: str,
        env_keys: Optional[List[str]] = None,
    ) -> None:
        await real_emit_open(session_id, session_ref, cwd, env_keys)
        await captured.put(session_ref)

    mgr.emit_devcontainer_session_open = _capturing_emit_open  # type: ignore[method-assign]

    open_task = asyncio.ensure_future(
        bridge.open_host_session(
            session_id=session_id, cwd="/work", env_whitelist={}, pre_spawn_guard=None,
        )
    )
    session_ref = await asyncio.wait_for(captured.get(), timeout=1.0)
    mgr.resolve_devcontainer_session_opened(session_ref, True, None)
    session = await asyncio.wait_for(open_task, timeout=1.0)
    return session, session_ref


def test_open_run_stream_exit_round_trip() -> None:
    mgr = ConnectionManager()
    bridge = WebSocketHostBridge(manager=mgr)

    async def scenario() -> None:
        session, session_ref = await _open_bridge_session(mgr, bridge, "s1")
        try:
            marker: bytes = session._framer.marker  # type: ignore[attr-defined]

            async def simulate_host_reply() -> None:
                # A real host's shell echoes the command's stdout, then the
                # sentinel line carrying the exit code — the same protocol
                # core.pty_session's real PTY produces.
                mgr.push_devcontainer_session_chunk(session_ref, _b64(b"hello\n"))
                mgr.push_devcontainer_session_chunk(session_ref, _b64(marker + b"0\n"))

            host_task = asyncio.ensure_future(simulate_host_reply())
            exit_code = await asyncio.wait_for(
                session.run("echo hello", timeout_s=2.0), timeout=2.0,
            )
            await host_task
            assert exit_code == 0

            stream_iter = session.stream()
            first_chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=1.0)
            assert first_chunk == b"hello\n"

            # Host reports the underlying shell exited.
            mgr.resolve_devcontainer_session_exit(session_ref, 0)
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(stream_iter.__anext__(), timeout=1.0)
        finally:
            await session.close()

    asyncio.run(scenario())


def test_disconnect_mid_run_resolves_via_timeout_not_a_hang() -> None:
    """The command's own timeout_s is the backstop when a disconnect drops the
    sentinel entirely — run() must never hang forever."""
    mgr = ConnectionManager()
    bridge = WebSocketHostBridge(manager=mgr)

    async def scenario() -> None:
        session, session_ref = await _open_bridge_session(mgr, bridge, "s1")
        run_task = asyncio.ensure_future(session.run("sleep 99", timeout_s=0.2))
        await asyncio.sleep(0)
        mgr._reap_client_state("s1")  # simulates an IDE disconnect mid-command
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=2.0)
        # The demux task's own cleanup ran too — nothing left registered.
        assert mgr.get_devcontainer_session_queue(session_ref) is None

    asyncio.run(scenario())


def test_cancelling_the_demux_task_still_delivers_eof_and_unregisters() -> None:
    """D1's binding cancellation contract: the demux task's `finally` runs on
    CancelledError exactly as it does on normal EOF or an exception."""
    mgr = ConnectionManager()
    bridge = WebSocketHostBridge(manager=mgr)

    async def scenario() -> None:
        session, session_ref = await _open_bridge_session(mgr, bridge, "s1")
        stream_iter = session.stream()
        task = session._demux_task  # type: ignore[attr-defined]
        assert task is not None
        task.cancel()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream_iter.__anext__(), timeout=1.0)
        assert mgr.get_devcontainer_session_queue(session_ref) is None
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_close_is_idempotent() -> None:
    mgr = ConnectionManager()
    bridge = WebSocketHostBridge(manager=mgr)

    async def scenario() -> None:
        session, session_ref = await _open_bridge_session(mgr, bridge, "s1")
        await session.close()
        await session.close()  # must not raise or double-emit
        assert mgr.get_devcontainer_session_queue(session_ref) is None

    asyncio.run(scenario())


def test_kill_signals_and_tears_down() -> None:
    mgr = ConnectionManager()
    bridge = WebSocketHostBridge(manager=mgr)

    async def scenario() -> None:
        session, session_ref = await _open_bridge_session(mgr, bridge, "s1")
        await session.kill()
        assert mgr.get_devcontainer_session_queue(session_ref) is None
        await session.close()  # still idempotent after kill()

    asyncio.run(scenario())
