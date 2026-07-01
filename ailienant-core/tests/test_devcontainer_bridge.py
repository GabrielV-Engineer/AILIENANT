"""WebSocketHostBridge — the concrete trusted-tier host bridge.

The bridge maps the `HostExecutionBridge` Protocol onto the `ConnectionManager`
transport primitives: it mints a `request_id`, emits the server→host request,
and awaits the host reply. These tests drive it with a **fake manager** (no real
socket) to prove the round-trips and the timeout/degrade mapping in isolation.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from api.devcontainer_bridge import WebSocketHostBridge


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


def test_open_host_session_raises_not_wired() -> None:
    from core.pty_session import SandboxSessionError

    mgr = _FakeManager()
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]

    async def _scenario() -> None:
        try:
            await bridge.open_host_session(
                session_id="s", cwd="/work", env_whitelist={}, pre_spawn_guard=None,
            )
        except SandboxSessionError:
            return
        raise AssertionError("expected SandboxSessionError")

    asyncio.run(_scenario())
