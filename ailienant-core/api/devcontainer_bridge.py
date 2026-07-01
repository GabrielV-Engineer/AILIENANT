# ailienant-core/api/devcontainer_bridge.py
"""Concrete host execution bridge for the trusted devcontainer tier.

The backend :class:`DevcontainerSandboxAdapter` never shells Docker itself: it
routes provisioning and command execution over a :class:`HostExecutionBridge` to
the IDE host, which owns the local container runtime. This module is that bridge
— the transport implementation that lives in the ``api`` layer and is injected
into ``core`` from the composition root (dependency inversion; ``core`` depends
only on the Protocol it owns).

It is stateless with respect to sessions: the session id is a per-call argument,
so a single instance serves every connected session. Each call correlates its
frames with a fresh ``request_id`` and awaits the matching host reply through the
``ConnectionManager`` transport primitives, which bound every wait and reap any
in-flight waiter on disconnect (so no path hangs).
"""

from __future__ import annotations

import uuid
from typing import Dict, Optional

from api.websocket_manager import ConnectionManager, vfs_manager
from core.sandbox import (
    _PROVISION_TIMEOUT_S,
    HostExecutionBridge,
    SandboxResult,
    SandboxSession,
)
from core.pty_session import PreSpawnGuard, SandboxSessionError


class WebSocketHostBridge(HostExecutionBridge):
    """Route ``ensure_provisioned`` / ``exec_command`` over the WS host channel.

    Wraps the global :class:`ConnectionManager` singleton (exported as
    ``vfs_manager``). The manager is an injectable constructor argument so a unit
    test can drive the bridge with a fake manager; production passes the default
    singleton.
    """

    def __init__(self, manager: Optional[ConnectionManager] = None) -> None:
        self._mgr: ConnectionManager = manager if manager is not None else vfs_manager

    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        """Ask the host to bring the devcontainer up; ``True`` when ready."""
        request_id = uuid.uuid4().hex
        await self._mgr.emit_devcontainer_provision_request(
            session_id=session_id, request_id=request_id, cwd=cwd,
        )
        state = await self._mgr.wait_devcontainer_provision(
            request_id=request_id, session_id=session_id, timeout=_PROVISION_TIMEOUT_S,
        )
        return state == "ready"

    async def exec_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        timeout_s: float,
    ) -> SandboxResult:
        """Run one command in the provisioned container and collect its output.

        ``env_whitelist`` is reduced to allowlisted variable **names** on the
        wire — never values; the host resolves the values from its own
        environment. The wait is bounded by ``timeout_s`` (below the adapter's
        outer ``timeout_s + _BRIDGE_GRACE_S`` guard), so the bridge settles first
        and returns a value rather than letting the outer wait race.
        """
        request_id = uuid.uuid4().hex
        await self._mgr.emit_devcontainer_exec_request(
            session_id=session_id,
            request_id=request_id,
            command=command,
            cwd=cwd,
            env_keys=list(env_whitelist.keys()),
        )
        result = await self._mgr.wait_devcontainer_exec(
            request_id=request_id, session_id=session_id, timeout=timeout_s,
        )
        if result is None:
            # Timeout or disconnect — the adapter maps this into its degrade path.
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_exec_no_reply]",
            )
        return SandboxResult(
            exit_code=int(result["exit_code"]),
            stdout=str(result["stdout"]),
            stderr=str(result["stderr"]),
        )

    async def open_host_session(
        self,
        *,
        session_id: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        pre_spawn_guard: Optional[PreSpawnGuard],
    ) -> SandboxSession:
        """Interactive devcontainer sessions are not yet wired over this bridge.

        The current WS contract covers one-shot exec only; ``run_command`` uses
        ``execute()`` and never reaches here. Raising keeps the Protocol total.
        """
        raise SandboxSessionError(
            "Interactive devcontainer sessions are not yet wired over the host bridge."
        )
