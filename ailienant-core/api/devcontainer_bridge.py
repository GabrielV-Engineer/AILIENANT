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

import asyncio
import uuid
from typing import AsyncIterator, Dict, Optional

from api.websocket_manager import ConnectionManager, vfs_manager
from core.command_boundary import CommandBoundaryFramer
from core.sandbox import (
    _PROVISION_TIMEOUT_S,
    HostExecutionBridge,
    SandboxResult,
    SandboxSession,
)
from core.pty_session import PreSpawnGuard, SandboxSessionError

# §43 tunables. The open handshake spawns a shell inside an already-provisioned
# container (no image build), so it is bounded far tighter than provisioning
# itself; the teardown join mirrors core.pty_session._JOIN_TIMEOUT_S so both
# session flavors give their demux consumer the same grace window before a
# forced cancel.
_SESSION_OPEN_TIMEOUT_S: float = 30.0
_SESSION_TEARDOWN_JOIN_TIMEOUT_S: float = 2.0


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
        """Open a persistent interactive session inside the provisioned container.

        Round-trips the §43 open handshake (``server_devcontainer_session_open``
        → ``client_devcontainer_session_opened``), then wraps the confirmed
        ``session_ref`` in a :class:`_BridgeSandboxSession` — the event-loop
        side of the tunnel, whose demux task consumes the manager's per-session
        inbound queue exactly as :class:`core.pty_session._PtySession` consumes
        its reader thread's queue, just fed from a socket instead of a PTY.

        A failed or timed-out open raises :class:`SandboxSessionError`,
        consistent with every other tier's ``open_session`` failure contract.
        """
        session_ref = uuid.uuid4().hex
        await self._mgr.emit_devcontainer_session_open(
            session_id=session_id, session_ref=session_ref, cwd=cwd,
            env_keys=list(env_whitelist.keys()),
        )
        result = await self._mgr.wait_devcontainer_session_opened(
            session_ref=session_ref, session_id=session_id, timeout=_SESSION_OPEN_TIMEOUT_S,
        )
        if result is None or not result.get("ok"):
            detail = (result or {}).get("detail") or "no reply from host"
            raise SandboxSessionError(
                f"Devcontainer interactive session failed to open: {detail}"
            )
        session = _BridgeSandboxSession(
            manager=self._mgr,
            session_id=session_id,
            session_ref=session_ref,
            pre_spawn_guard=pre_spawn_guard,
        )
        await session.start()
        return session


class _BridgeSandboxSession(SandboxSession):
    """Persistent interactive session tunneled over the host bridge (§43).

    A bidirectional analogue of :class:`core.pty_session._PtySession` running
    the same sentinel-marker command-boundary protocol (shared via
    :class:`~core.command_boundary.CommandBoundaryFramer`) over a different
    transport: instead of a local reader thread draining a real PTY, inbound
    bytes arrive on the shared WS receive loop and are handed off through a
    manager-owned bounded queue
    (:meth:`ConnectionManager.push_devcontainer_session_chunk`). This class's
    own demux task consumes that queue — same framing, same boundary-resolves-
    a-pending-future discipline, just fed from a socket instead of a thread.

    Cancellation safety (binding, not incidental): the demux task's ``finally``
    unconditionally forces the ``stream()`` EOF sentinel and unregisters this
    session's manager-side bookkeeping — on normal EOF, on an unexpected
    exception, and on ``asyncio.CancelledError`` alike — so a consumer awaiting
    :meth:`stream` or a suspended :meth:`run` can never be left stranded, and a
    disconnect mid-session (reaped by ``ConnectionManager._reap_client_state``,
    which force-EOFs the queue) resolves this session rather than hanging it.
    """

    def __init__(
        self,
        *,
        manager: ConnectionManager,
        session_id: str,
        session_ref: str,
        shell_kind: str = "posix",
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> None:
        self._mgr = manager
        self._session_id = session_id
        self._session_ref = session_ref
        self._pre_spawn_guard = pre_spawn_guard
        self._framer = CommandBoundaryFramer(shell_kind=shell_kind)
        self._out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=256)
        self._demux_task: Optional["asyncio.Task[None]"] = None
        self._pending: Optional["asyncio.Future[int]"] = None
        self._started = False
        self._closing = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Idempotent. The open handshake already succeeded by construction
        time (:meth:`WebSocketHostBridge.open_host_session`); this only wires
        the local demux consumer onto the manager's already-registered queue.
        """
        if self._started:
            return
        self._demux_task = asyncio.ensure_future(self._demux_loop())
        self._started = True

    # ── demux (on the event loop — no reader thread; the transport is the WS
    #    receive loop itself, which feeds the manager's per-session queue) ────

    async def _demux_loop(self) -> None:
        queue = self._mgr.get_devcontainer_session_queue(self._session_ref)
        buf = bytearray()
        try:
            if queue is None:
                # Registered by wait_devcontainer_session_opened before this
                # session was even constructed; its absence means the open
                # already failed and was cleaned up — nothing to consume.
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                # Check for a pause->resume transition on every drain, not only
                # on a producer push: once paused and full, nothing pushes
                # again if the child's output has genuinely stalled — and the
                # child is exactly the thing stalled, blocked writing to its
                # own stdout pipe because the host never resumed reading it.
                # A producer-only check would deadlock right here.
                resume = self._mgr.check_devcontainer_session_drain(self._session_ref)
                if resume is not None:
                    await self._mgr.emit_devcontainer_session_flow(
                        session_id=self._session_id, session_ref=self._session_ref,
                        paused=resume,
                    )
                buf.extend(item)
                emit, buf, codes = self._framer.drain_boundaries(buf)
                for code in codes:
                    self._resolve(code)
                if emit:
                    await self._out_q.put(bytes(emit))
        finally:
            # Unconditional — runs on normal EOF, on exception, AND on
            # CancelledError (D1). Both calls are synchronous (put_nowait /
            # dict.pop+set.discard), so this finally can never itself stall or
            # be caught mid-cleanup by a further cancellation.
            self._force_out_eof()
            self._mgr.unregister_devcontainer_session(self._session_id, self._session_ref)

    def _force_out_eof(self) -> None:
        """Best-effort guarantee that a stalled :meth:`stream` sees its EOF
        sentinel — mirrors ``core.pty_session._PtySession._force_out_eof``."""
        try:
            self._out_q.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._out_q.get_nowait()
                self._out_q.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def _resolve(self, code: int) -> None:
        fut = self._pending
        if fut is not None and not fut.done():
            fut.set_result(code)
        self._pending = None

    # ── public API ───────────────────────────────────────────────────────────

    async def run(self, command: str, *, timeout_s: float) -> int:
        if not self._started:
            raise SandboxSessionError("session not started")
        if self._pre_spawn_guard is not None:
            reason = self._pre_spawn_guard(command)
            if reason is not None:
                raise SandboxSessionError(f"command vetoed pre-spawn: {reason}")
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        await self._mgr.emit_devcontainer_session_stdin(
            session_id=self._session_id, session_ref=self._session_ref,
            data=self._framer.compose(command),
        )
        try:
            return await asyncio.wait_for(self._pending, timeout=timeout_s)
        finally:
            # A timed-out run must not leave a stale future for a LATER
            # boundary to resolve into (the next run() creates a fresh one
            # regardless, but this keeps no dangling reference around).
            self._pending = None

    def stream(self) -> AsyncIterator[bytes]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._out_q.get()
            if item is None:
                return
            yield item

    async def write_stdin(self, data: bytes) -> None:
        await self._mgr.emit_devcontainer_session_stdin(
            session_id=self._session_id, session_ref=self._session_ref, data=data,
        )

    async def interrupt(self) -> None:
        await self._mgr.emit_devcontainer_session_signal(
            session_id=self._session_id, session_ref=self._session_ref, signal="interrupt",
        )

    async def kill(self) -> None:
        await self._teardown(graceful=False)

    async def close(self) -> None:
        await self._teardown(graceful=True)

    # ── teardown ─────────────────────────────────────────────────────────────

    async def _teardown(self, *, graceful: bool) -> None:
        """Idempotent and cancellation-proof — safe to call twice, and safe to
        call from a ``finally`` in an already-cancelled caller."""
        if self._closing:
            return
        self._closing = True
        try:
            if graceful:
                await self._mgr.emit_devcontainer_session_close(
                    session_id=self._session_id, session_ref=self._session_ref,
                )
            else:
                await self._mgr.emit_devcontainer_session_signal(
                    session_id=self._session_id, session_ref=self._session_ref, signal="kill",
                )
        except Exception:  # noqa: BLE001 — a WS send failure must not block cleanup below
            pass
        task = self._demux_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_SESSION_TEARDOWN_JOIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                task.cancel()
                self._force_out_eof()
            except asyncio.CancelledError:
                pass
        # Idempotent — safe even though the demux task's own finally already
        # ran this (or will, once its cancellation unwinds): a session whose
        # start() was never called (task is None) still needs this to run.
        self._mgr.unregister_devcontainer_session(self._session_id, self._session_ref)
