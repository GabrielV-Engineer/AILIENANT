# ailienant-core/core/sandbox.py
"""Phase 6.1.1 + 6.1.2 — Pluggable Sandbox Adapter (ABC + Docker + NativeHITL).

Implements the host-isolation primitive defined in
``docs/PHASE_6_BLUEPRINT.md §2``. Today every EXECUTE-tier tool call hits
``asyncio.create_subprocess_shell`` against the host with full parent
privileges — this module lands the adapter contract plus the concretes that
the Phase 6.1.4 resolver will pick from at startup.

Implemented here:
    * :class:`SandboxAdapter` — the ABC (Phase 6.1.1).
    * :class:`DockerSandboxAdapter` — default tier when the Docker daemon is
      reachable (Phase 6.1.1).
    * :class:`NativeHITLSandboxAdapter` — degraded-mode fallback gated by the
      canonical ``vfs_manager.request_human_approval`` channel (Phase 6.1.2).
    * :class:`WasmSandboxAdapter` — pure-compute tier on a ``wasmtime`` WASI
      runtime, fuel-metered and preopen-free (Phase 6.1.3).
    * :func:`resolve_default_adapter` — the startup probe that picks a tier
      (Docker → Wasm → NativeHITL) and binds the ``ACTIVE_TIER`` /
      ``ACTIVE_ADAPTER`` globals, read back via :func:`get_active_tier` /
      :func:`get_active_adapter` (Phase 6.1.4).

Out of scope for this module (deferred to later sub-tasks of Phase 6):
    * Dispatch swap in ``tools/execution_tools.py`` (Phase 6.2)

All synchronous ``docker`` SDK calls are wrapped in :func:`asyncio.to_thread` to
protect the FastAPI event loop — same discipline as :mod:`core.janitor`.
Docker-tier timeouts are enforced **inside** the container by the GNU
``timeout`` coreutils (SIGTERM then SIGKILL); the NativeHITL tier enforces
timeouts host-side via :func:`asyncio.wait_for` and reaps the OS process to
prevent zombie accumulation. The Wasm tier blocks the CPU during module
compilation + execution, so both run inside :func:`asyncio.to_thread`; runaway
payloads are bounded by a 5 M-instruction fuel cap rather than wall-clock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from abc import ABC, abstractmethod
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Protocol, Tuple

if TYPE_CHECKING:
    from core.workspace_sync import SyncSurface

import docker
import docker.errors  # explicit submodule import so the type checker resolves docker.errors.*
import wasmtime
from pydantic import BaseModel

from core.pty_session import (
    PreSpawnGuard,
    SandboxSession,
    SandboxSessionError,
    _PtyBackend,
    _PtySession,
)

logger = logging.getLogger("AILIENANT_SANDBOX")

# ── Module constants ─────────────────────────────────────────────────────────

_SANDBOX_IMAGE_TAG: str = "ailienant-sandbox:latest"
_SANDBOX_CONTAINER_NAME: str = "ailienant-sandbox-daemon"
_SANDBOX_REMOTE_REPO: str = "ghcr.io/gabrielv-engineer/ailienant-sandbox"
_SANDBOX_REMOTE_TAG: str = "latest"
_CONTAINER_WORKDIR: str = "/workspace"
_CONTAINER_TMPFS_PATH: str = "/work"
_DEFAULT_BUILD_TIMEOUT_S: int = 600
_DEFAULT_EXEC_TIMEOUT_S: int = 30

_DOCKERFILE_TEXT: str = (
    "FROM python:3.13-slim\n"
    "RUN useradd --create-home --uid 1000 sandbox \\\n"
    " && mkdir -p /work \\\n"
    " && chown sandbox:sandbox /work\n"
    "USER sandbox\n"
    "WORKDIR /workspace\n"
    "ENV PYTHONDONTWRITEBYTECODE=1 \\\n"
    "    PYTHONUNBUFFERED=1 \\\n"
    "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\\n"
    "    RUFF_CACHE_DIR=/work/.ruff_cache \\\n"
    "    MYPY_CACHE_DIR=/work/.mypy_cache\n"
    'CMD ["tail", "-f", "/dev/null"]\n'
)

# ── Wasm tier constants (Phase 6.1.3) ────────────────────────────────────────

_WASM_FUEL_LIMIT: int = 5_000_000          # ADR-002 hard instruction cap
_WASM_ENTRYPOINT: str = "_start"           # WASI command-module entrypoint
_WASM_ALLOWED_IMPORT_MODULES: frozenset[str] = frozenset(
    {"wasi_snapshot_preview1"}             # WASI-preview1 only — no custom host
)


# ── Pydantic result model ────────────────────────────────────────────────────


class SandboxResult(BaseModel):
    """Minimal sandbox-execution outcome (Phase 6.1.1 fields only).

    Additional fields (``sandbox_tier``, ``duration_ms``, ``audit_id``) are
    deliberately deferred to the consumer layer per
    ``PHASE_6_BLUEPRINT.md §2.1``.
    """

    exit_code: int
    stdout: str
    stderr: str


# ── Abstract contract ────────────────────────────────────────────────────────


class SandboxAdapter(ABC):
    """Base contract for every Phase 6 sandbox tier.

    Concrete implementations:
        * :class:`DockerSandboxAdapter` (Phase 6.1.1, this module)
        * :class:`NativeHITLSandboxAdapter` (Phase 6.1.2, this module)
        * :class:`WasmSandboxAdapter` (Phase 6.1.3, this module)
    """

    @abstractmethod
    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """Run ``command`` inside the adapter's isolation envelope.

        ``env_whitelist`` is the **only** environment dictionary the command
        sees — host environment (including API keys) MUST NOT leak through.

        ``session_id`` is consumed by adapters that route through the HITL
        channel (Phase 6.1.2 :class:`NativeHITLSandboxAdapter`). Adapters that
        own their isolation envelope end-to-end (Docker, Wasm) accept the
        kwarg for LSP parity and ignore it.
        """
        ...

    supports_sessions: bool = False
    """Whether the tier can open a persistent interactive :class:`SandboxSession`.

    ``False`` on the base so non-interactive tiers (pure-compute Wasm) need no
    override; session-capable tiers set it ``True`` and override
    :meth:`open_session`. A dispatcher branches on this flag rather than
    catching ``NotImplementedError``.
    """

    async def open_session(
        self,
        *,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> SandboxSession:
        """Open a persistent interactive shell that survives across commands.

        Default implementation refuses: tiers without interactive I/O do not
        override it. Overriding tiers must also set
        :attr:`supports_sessions` to ``True``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive sessions."
        )

    def get_sync_surface(self, cwd: str) -> "SyncSurface":
        """Return the writable SyncSurface for this adapter.

        Session-capable tiers (Docker, NativeDirect) override this. Tiers
        without an interactive work surface (Wasm, HITL) inherit the default
        which raises, consistent with the open_session pattern.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose a sync surface."
        )


# ── Docker concrete adapter ──────────────────────────────────────────────────


class DockerSandboxAdapter(SandboxAdapter):
    """Long-lived ``ailienant-sandbox-daemon`` container; ``docker exec`` per call.

    Security profile (locked to ``PHASE_6_BLUEPRINT.md §2.2``):

    * ``--read-only`` rootfs
    * ``--network none``
    * host CWD bind-mounted at ``/workspace`` read-only
    * ``tmpfs`` at ``/work`` (512 MB, ``nosuid``, ``nodev``)
    * Environment filtered to the per-call ``env_whitelist``

    Known limit (R5 in the plan): a hung Docker daemon will still block the
    worker thread because the synchronous SDK call cannot be interrupted from
    Python. The Phase 6.1.4 resolver will surface this via a startup probe.
    """

    supports_sessions = True

    def __init__(self, *, host_workspace: Optional[str] = None) -> None:
        self._client: Optional[Any] = None
        self._container: Optional[Any] = None
        self._image_id: Optional[str] = None
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._host_workspace: str = host_workspace or os.getcwd()

    @property
    def host_workspace(self) -> str:
        """The host directory bind-mounted read-only at ``/workspace``.

        The single authority for the mount root: any host path written under
        this directory is visible inside the container and is translated into
        ``/workspace/…`` by :meth:`_translate_cwd`. Consumers that must place a
        file where the container can read it (e.g. the multi-file benchmark
        oracle) materialize under this root rather than re-deriving the working
        directory, which could drift from what was actually mounted.
        """
        return self._host_workspace

    # ── public API ──────────────────────────────────────────────────────────

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """Dispatch ``command`` to the sandbox container.

        Timeout is enforced **inside** the container by the GNU ``timeout``
        coreutils; the kernel SIGTERMs (then SIGKILLs after 1 s grace) the
        process group when the deadline expires and ``exec_run`` returns
        naturally with exit code 124, freeing the Python worker thread
        instantly.

        ``session_id`` is accepted for ABC parity and intentionally ignored —
        the Docker tier owns its isolation envelope.
        """
        del session_id  # ABC parity; the Docker tier does not need it.
        await self._ensure_container_running()
        assert self._container is not None  # narrowed by lock-guarded init

        container_cwd = self._translate_cwd(cwd)
        wrapped = (
            f"timeout --foreground -k 1 {int(timeout_s)}s "
            f"sh -c {shlex.quote(command)}"
        )

        exit_code, output = await asyncio.to_thread(
            self._exec_command_sync, wrapped, container_cwd, env_whitelist,
        )

        stdout_bytes, stderr_bytes = self._split_output(output)
        stdout = self._decode(stdout_bytes)
        stderr = self._decode(stderr_bytes)

        if exit_code == 124:
            # GNU timeout convention. A user command legitimately exiting 124
            # is indistinguishable here — a known coreutils limitation,
            # accepted for 6.1.1; consumers can read stderr for confirmation.
            timeout_note = (
                f"[sandbox_timeout] command exceeded {int(timeout_s)}s wall clock"
            )
            stderr = f"{timeout_note}\n{stderr}" if stderr else timeout_note

        return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def open_session(
        self,
        *,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> SandboxSession:
        """Open a persistent ``sh`` inside the daemon container over an exec socket.

        The exec is created with a TTY so the stream is raw (no 8-byte demux
        header) and line discipline is real, matching the host PTY model.
        """
        del session_id  # session identity is the dispatcher's concern
        await self._ensure_container_running()
        assert self._container is not None and self._client is not None
        client = self._client
        container_id = self._container.id
        container_cwd = self._translate_cwd(cwd)

        def _factory(
            argv: List[str], _cwd: str, env: Dict[str, str], _marker: bytes,
        ) -> _PtyBackend:
            return _DockerPtyBackend(client, container_id, container_cwd, env, argv)

        session = _PtySession(
            cwd=container_cwd,
            env=dict(env_whitelist),
            shell_kind="posix",
            pre_spawn_guard=pre_spawn_guard,
            backend_factory=_factory,
        )
        await session.start()
        return session

    def get_sync_surface(self, cwd: str) -> "SyncSurface":
        """Return a DockerSyncSurface targeting /work (the container's tmpfs)."""
        if self._container is None:
            raise RuntimeError(
                "DockerSandboxAdapter: container is not running; "
                "call _ensure_container_running() first."
            )
        from core.workspace_sync import DockerSyncSurface
        return DockerSyncSurface(self._container, _CONTAINER_TMPFS_PATH)

    async def shutdown(self) -> None:
        """Stop + remove the named container and close the Docker client.

        Idempotent — safe to call from the Phase 4.4 workspace teardown hook
        whether the container was ever started or not.
        """
        async with self._lifecycle_lock:
            container = self._container
            if container is not None:
                try:
                    await asyncio.to_thread(container.stop, timeout=10)
                except Exception as exc:  # noqa: BLE001 — defensive cleanup
                    logger.warning("Sandbox container stop failed: %s", exc)
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as exc:  # noqa: BLE001 — defensive cleanup
                    logger.warning("Sandbox container remove failed: %s", exc)
                self._container = None
            client = self._client
            if client is not None:
                try:
                    await asyncio.to_thread(client.close)
                except Exception as exc:  # noqa: BLE001 — defensive cleanup
                    logger.warning("Docker client close failed: %s", exc)
                self._client = None

    # ── lifecycle (lock-guarded) ────────────────────────────────────────────

    async def _ensure_container_running(self) -> None:
        """Lazy + idempotent: connect the SDK, build the image, start the container."""
        async with self._lifecycle_lock:
            if self._client is None:
                self._client = await asyncio.to_thread(docker.from_env)
            client = self._client
            assert client is not None

            if not await asyncio.to_thread(self._image_exists, client):
                logger.info(
                    "Building %s — first-run cost, ~30-60s",
                    _SANDBOX_IMAGE_TAG,
                )
                image = await asyncio.to_thread(self._build_image_sync, client)
                self._image_id = image.id

            existing = await asyncio.to_thread(self._get_existing_container, client)
            if existing is None:
                self._container = await asyncio.to_thread(
                    self._start_container_sync, client,
                )
            elif getattr(existing, "status", None) != "running":
                await asyncio.to_thread(existing.remove, force=True)
                self._container = await asyncio.to_thread(
                    self._start_container_sync, client,
                )
            else:
                self._container = existing

    # ── sync helpers (always called via asyncio.to_thread) ──────────────────

    def _image_exists(self, client: Any) -> bool:
        try:
            client.images.get(_SANDBOX_IMAGE_TAG)
            return True
        except docker.errors.ImageNotFound:
            return False

    def _build_image_sync(self, client: Any) -> Any:
        image, _logs = client.images.build(
            fileobj=BytesIO(_DOCKERFILE_TEXT.encode("utf-8")),
            tag=_SANDBOX_IMAGE_TAG,
            rm=True,
            forcerm=True,
            pull=True,
            timeout=_DEFAULT_BUILD_TIMEOUT_S,
        )
        return image

    def _get_existing_container(self, client: Any) -> Optional[Any]:
        try:
            return client.containers.get(_SANDBOX_CONTAINER_NAME)
        except docker.errors.NotFound:
            return None

    def _start_container_sync(self, client: Any) -> Any:
        return client.containers.run(
            _SANDBOX_IMAGE_TAG,
            command=["tail", "-f", "/dev/null"],
            name=_SANDBOX_CONTAINER_NAME,
            detach=True,
            read_only=True,
            network_mode="none",
            volumes={
                self._host_workspace: {
                    "bind": _CONTAINER_WORKDIR,
                    "mode": "ro",
                },
            },
            tmpfs={_CONTAINER_TMPFS_PATH: "rw,size=512m,nosuid,nodev"},
            working_dir=_CONTAINER_WORKDIR,
        )

    def _exec_command_sync(
        self,
        wrapped_command: str,
        container_cwd: str,
        env_whitelist: Dict[str, str],
    ) -> Tuple[int, Any]:
        container = self._container
        assert container is not None
        result = container.exec_run(
            wrapped_command,
            workdir=container_cwd,
            environment=dict(env_whitelist),
            demux=True,
            tty=False,
            stdout=True,
            stderr=True,
        )
        return int(result.exit_code), result.output

    # ── pure helpers (no I/O) ───────────────────────────────────────────────

    def _translate_cwd(self, host_cwd: str) -> str:
        """Map a host absolute path under ``self._host_workspace`` into ``/workspace``.

        Falls back to the container workdir if the path escapes the mount —
        defence in depth against a stale ``cwd`` from a different workspace.
        """
        if not host_cwd:
            return _CONTAINER_WORKDIR
        host_abs = os.path.abspath(host_cwd)
        root_abs = os.path.abspath(self._host_workspace)
        if host_abs == root_abs:
            return _CONTAINER_WORKDIR
        if host_abs.startswith(root_abs + os.sep):
            relative = host_abs[len(root_abs):].replace(os.sep, "/")
            return f"{_CONTAINER_WORKDIR}{relative}"
        logger.warning(
            "Sandbox cwd %r escapes host workspace %r — falling back to %s",
            host_cwd, self._host_workspace, _CONTAINER_WORKDIR,
        )
        return _CONTAINER_WORKDIR

    def _split_output(self, output: Any) -> Tuple[bytes, bytes]:
        """Normalise ``exec_run`` output to a ``(stdout, stderr)`` byte pair.

        With ``demux=True`` the SDK returns ``(stdout_or_None, stderr_or_None)``;
        in failure modes it may return raw ``bytes`` or ``None``.
        """
        if output is None:
            return b"", b""
        if isinstance(output, tuple):
            stdout = output[0] if len(output) > 0 and output[0] is not None else b""
            stderr = output[1] if len(output) > 1 and output[1] is not None else b""
            return bytes(stdout), bytes(stderr)
        if isinstance(output, (bytes, bytearray)):
            return bytes(output), b""
        return b"", b""

    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace") if raw else ""


# ── Native HITL fallback adapter ─────────────────────────────────────────────


class NativeHITLSandboxAdapter(SandboxAdapter):
    """Degraded-mode adapter: host-native subprocess gated by a human approval.

    Selected by the Phase 6.1.4 resolver only when neither Docker nor Wasm is
    available. Every call suspends until ``vfs_manager.request_human_approval``
    returns; rejection or timeout aborts cleanly without spawning anything.
    Approved commands run with :func:`asyncio.create_subprocess_shell` and
    inherit *only* ``env_whitelist`` — host environment (including API keys)
    MUST NOT leak through.

    Known limits (parity with R5 of the Docker tier):
        * ``process.kill()`` does not traverse the process tree on POSIX and
          maps to ``TerminateProcess`` on Windows (single-PID semantics). A
          shell-spawned command that forks long-lived children may leak them.
          Documented; out of scope for 6.1.2. A future ``setsid``/``killpg``
          POSIX path and ``CREATE_NEW_PROCESS_GROUP`` Windows path can be
          added in 6.1.2.b if telemetry shows orphan accumulation.
    """

    _HITL_ACTION: str = "SANDBOX_DEGRADED_EXEC"
    _HITL_TIMEOUT_S: float = 300.0  # matches resource_manager + finops defaults

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """HITL-gated host execution.

        Returns immediately with ``exit_code=-1`` if no session is available,
        the human declines, or the approval times out. Approved commands then
        run host-native with the timeout enforced via :func:`asyncio.wait_for`
        plus ``process.kill()`` and a ``process.wait()`` reap.
        """
        # Deferred import: api.websocket_manager imports from core.* at module
        # load, so a top-level import here re-creates the circular dependency
        # that resource_manager.py:171 already documented and side-stepped.
        from api.websocket_manager import vfs_manager

        if not session_id:
            logger.error(
                "NativeHITL adapter invoked without session_id — refusing to "
                "execute on host. Command suppressed: %s", command,
            )
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[hitl_no_session]",
            )

        approval = await vfs_manager.request_human_approval(
            session_id=session_id,
            action_description=self._HITL_ACTION,
            proposed_content=f"CWD: {cwd}\nCommand: {command}",
            timeout_s=self._HITL_TIMEOUT_S,
            request_kind=self._HITL_ACTION,
        )
        if approval is None or not approval.get("approved", False):
            # None ⇒ HITL timeout; approved=False ⇒ explicit rejection.
            # Both are non-events: nothing was spawned.
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[hitl_denied]",
            )

        return await self._spawn_with_timeout(
            command=command,
            timeout_s=timeout_s,
            cwd=cwd,
            env_whitelist=env_whitelist,
        )

    async def _spawn_with_timeout(
        self,
        *,
        command: str,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
    ) -> SandboxResult:
        """Host-side spawn with strict timeout + zombie reaping."""
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd or None,
            env=dict(env_whitelist),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()  # reap — prevents OS zombie
            await self._enqueue_dlq_stub(command=command, cwd=cwd)
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[hitl_native_timeout]",
            )

        exit_code = process.returncode if process.returncode is not None else -1
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def _enqueue_dlq_stub(self, *, command: str, cwd: str) -> None:
        """Phase 6.4 hand-off stub.

        Logs a CRITICAL line that the Phase 6.4 DLQ ingestor will retrofit by
        log-tail or by a shared in-memory queue once it lands. We intentionally
        do NOT enqueue to a real queue here — that would require a state-channel
        addition and 6.1.2 is locked to no-state-channel-changes.
        """
        logger.critical(
            "[DLQ:NativeHITL] timeout — command suppressed for replay. "
            "cwd=%s command=%s", cwd, command,
        )


# ── Devcontainer trusted-tier adapter ────────────────────────────────────────

_PROVISION_TIMEOUT_S: int = 600   # devcontainer up is minutes-long (image build + caching)
_BRIDGE_GRACE_S: float = 1.0      # outer wall-clock margin over the host-side exec timeout


class HostExecutionBridge(Protocol):
    """Structural contract for the off-process host that owns the devcontainer.

    The adapter routes trusted execution to whichever component drives the
    user's local container runtime (the IDE host). A ``Protocol`` — not an ABC —
    keeps that component free to satisfy this contract structurally without
    importing back into this module, so the implementor can live in the host
    layer that already depends on ``core`` without forming an import cycle.

    Implementors own the wire encoding and the actual ``devcontainer up`` /
    ``devcontainer exec`` invocation. ``env_whitelist`` is applied by the host
    when launching the command, so no secret values are threaded through any
    persisted state here.
    """

    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        """Bring the workspace container up (idempotent host-side); ``True`` when ready."""
        ...

    async def exec_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        timeout_s: float,
    ) -> SandboxResult:
        """Run a one-shot command inside the provisioned container."""
        ...

    async def open_host_session(
        self,
        *,
        session_id: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        pre_spawn_guard: Optional[PreSpawnGuard],
    ) -> SandboxSession:
        """Open a persistent interactive session inside the provisioned container."""
        ...


def _default_host_bridge() -> Optional[HostExecutionBridge]:
    """Resolve the process-wide host bridge.

    Returns ``None`` until a concrete host bridge is wired in, which is the
    point at which trusted execution becomes routable. A ``None`` result makes
    the adapter degrade cleanly rather than crash, so the tier is safe to
    construct before the host channel exists.
    """
    return None


class DevcontainerSandboxAdapter(SandboxAdapter):
    """Trusted-tier adapter that routes execution to a user-owned devcontainer.

    Where :class:`DockerSandboxAdapter` is a locked cage for *untrusted* model
    output, this tier reproduces the user's *own* project environment declared
    in ``devcontainer.json``. It never shells Docker itself: every command is
    routed over a :class:`HostExecutionBridge` to the host, which owns the
    container runtime. Provisioning is lazy, idempotent and single-flight; a
    missing bridge or a provisioning / execution timeout degrades to a bracketed
    sentinel (plus a dead-letter log line) and never crashes the host process —
    the same off-process discipline as :class:`NativeHITLSandboxAdapter`.
    """

    supports_sessions = True

    def __init__(
        self,
        *,
        bridge: Optional[HostExecutionBridge] = None,
        host_workspace: Optional[str] = None,
    ) -> None:
        self._bridge: Optional[HostExecutionBridge] = bridge
        self._host_workspace: str = host_workspace or os.getcwd()
        self._provision_lock: asyncio.Lock = asyncio.Lock()
        self._provisioned: bool = False

    @property
    def host_workspace(self) -> str:
        """The workspace root that holds ``devcontainer.json`` and is provisioned."""
        return self._host_workspace

    def _get_bridge(self) -> Optional[HostExecutionBridge]:
        """Resolve the bridge per call: an injected instance wins, else the default."""
        return self._bridge if self._bridge is not None else _default_host_bridge()

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """Route ``command`` to the host container; always returns, never raises.

        Every failure mode collapses to ``exit_code=-1`` with a bracketed
        sentinel so a host or bridge fault can never crash the backend process.
        """
        if not session_id:
            logger.error(
                "Devcontainer adapter invoked without session_id — refusing to "
                "route to host. Command suppressed: %s", command,
            )
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_no_session]",
            )

        bridge = self._get_bridge()
        if bridge is None:
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_bridge_unavailable]",
            )

        try:
            provisioned = await self._ensure_provisioned(
                bridge=bridge, session_id=session_id, cwd=cwd,
            )
        except asyncio.TimeoutError:
            await self._enqueue_dlq_stub(command="devcontainer up", cwd=cwd)
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_provision_timeout]",
            )
        if not provisioned:
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_provision_failed]",
            )

        try:
            return await asyncio.wait_for(
                bridge.exec_command(
                    session_id=session_id,
                    command=command,
                    cwd=cwd,
                    env_whitelist=env_whitelist,
                    timeout_s=timeout_s,
                ),
                timeout=timeout_s + _BRIDGE_GRACE_S,
            )
        except asyncio.TimeoutError:
            await self._enqueue_dlq_stub(command=command, cwd=cwd)
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_exec_timeout]",
            )
        except Exception as exc:  # noqa: BLE001 — a host/bridge fault must not crash the backend
            logger.error(
                "Devcontainer bridge exec failed: %s", exc, exc_info=True,
            )
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[devcontainer_bridge_error]",
            )

    async def open_session(
        self,
        *,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> SandboxSession:
        """Open a persistent interactive session over the host bridge.

        Unlike :meth:`execute` (which returns a degrade result), an interactive
        open is exceptional on failure: a missing bridge or a failed/timed-out
        provision raises :class:`SandboxSessionError`.
        """
        if not session_id:
            raise SandboxSessionError(
                "DevcontainerSandboxAdapter.open_session requires a session_id."
            )
        bridge = self._get_bridge()
        if bridge is None:
            raise SandboxSessionError(
                "Devcontainer host bridge unavailable — cannot open a session."
            )
        try:
            provisioned = await self._ensure_provisioned(
                bridge=bridge, session_id=session_id, cwd=cwd,
            )
        except asyncio.TimeoutError as exc:
            await self._enqueue_dlq_stub(command="devcontainer up", cwd=cwd)
            raise SandboxSessionError(
                "Devcontainer provisioning timed out — cannot open a session."
            ) from exc
        if not provisioned:
            raise SandboxSessionError(
                "Devcontainer provisioning failed — cannot open a session."
            )
        return await bridge.open_host_session(
            session_id=session_id,
            cwd=cwd,
            env_whitelist=env_whitelist,
            pre_spawn_guard=pre_spawn_guard,
        )

    async def _ensure_provisioned(
        self,
        *,
        bridge: HostExecutionBridge,
        session_id: str,
        cwd: str,
    ) -> bool:
        """Lazy, idempotent, single-flight ``devcontainer up`` over the bridge.

        Re-entry after a successful provision is a fast-path no-op. The slow
        provisioning ``await`` runs inside the lock so concurrent callers
        serialize behind a single attempt. A timeout propagates to the caller
        (which picks the right degrade); a ``False`` result is deliberately not
        latched, so the next call retries.
        """
        if self._provisioned:
            return True
        async with self._provision_lock:
            if self._provisioned:
                return True
            ready = await asyncio.wait_for(
                bridge.ensure_provisioned(session_id=session_id, cwd=cwd),
                timeout=_PROVISION_TIMEOUT_S,
            )
            if ready:
                self._provisioned = True
            return ready

    async def _enqueue_dlq_stub(self, *, command: str, cwd: str) -> None:
        """Dead-letter hand-off stub: log a CRITICAL line for later replay.

        No real queue is written here — that requires a state-channel addition
        out of scope for this tier — mirroring the NativeHITL DLQ stub.
        """
        logger.critical(
            "[DLQ:Devcontainer] host execution suppressed for replay. "
            "cwd=%s command=%s", cwd, command,
        )


# ── Wasm pure-compute adapter ────────────────────────────────────────────────


class WasmScopeError(Exception):
    """Raised by the ADR-002 Scope Guard when a ``.wasm`` payload imports a
    host module outside the WASI-preview1 allow-list.

    Public so the Phase 6.10 B1 adversarial test and the future
    ``RunPureLogicTool`` consumer can assert against it directly.
    """

    def __init__(self, import_module: str, import_name: str) -> None:
        self.import_module = import_module
        self.import_name = import_name
        super().__init__(
            f"disallowed wasm import: {import_module}::{import_name}"
        )


class WasmSandboxAdapter(SandboxAdapter):
    """Pure-compute tier: runs a pre-compiled ``.wasm`` payload under WASI.

    Strongest isolation of the three tiers — a WASI-preview1 module granted
    **zero preopens** structurally cannot reach the host filesystem or
    network (capability model), independent of any daemon or human. The
    trade-off: compute only — no ``pytest`` discovery, no ``tsc``/``npm``.

    Determinism + safety knobs (ADR-002, ``PHASE_6_BLUEPRINT.md §2.2``):

    * ``Config.consume_fuel`` + ``Store.set_fuel(5_000_000)`` — a runaway or
      infinite-loop payload traps once fuel is exhausted instead of hanging;
      fuel — not wall-clock — is the hard bound, so no worker thread can leak
      (contrast Docker R5 and NativeHITL N1).
    * No ``preopen_dir`` / no ``--mapdir`` — the guest sees only fds 0/1/2.
      stdout/stderr are redirected to **host** temp files (the host owns
      them; the guest is never handed a directory capability), then read
      back and unlinked.
    * Scope Guard: the module import section is inspected *before* fuel is
      set; any import outside the WASI-preview1 allow-list raises
      :class:`WasmScopeError`.

    ``timeout_s``, ``cwd`` and ``session_id`` are accepted for ABC parity and
    ignored — the Wasm tier has no wall-clock kill, no filesystem cwd, and no
    HITL surface.
    """

    def __init__(self) -> None:
        config = wasmtime.Config()
        config.consume_fuel = True
        self._engine: wasmtime.Engine = wasmtime.Engine(config)

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """Run the ``.wasm`` payload at the path given by ``command``.

        ``command`` is the path to a compiled ``.wasm`` file. Module
        compilation and execution are both CPU-bound and run inside
        :func:`asyncio.to_thread` so the FastAPI event loop is never blocked.
        """
        del timeout_s, cwd, session_id  # fuel is the bound; no FS/HITL surface
        return await asyncio.to_thread(
            self._run_sync, command, dict(env_whitelist),
        )

    # ── sync worker (always via asyncio.to_thread) ──────────────────────────

    def _run_sync(
        self, wasm_path: str, env_whitelist: Dict[str, str],
    ) -> SandboxResult:
        """Compile → scope-guard → fuel-meter → run; never raises."""
        if not os.path.isfile(wasm_path):
            return SandboxResult(
                exit_code=-1, stdout="",
                stderr=f"[wasm_load_error: file not found: {wasm_path}]",
            )
        try:
            module = wasmtime.Module.from_file(self._engine, wasm_path)
        except wasmtime.WasmtimeError as exc:
            return SandboxResult(
                exit_code=-1, stdout="", stderr=f"[wasm_load_error: {exc}]",
            )

        try:
            self._inspect_module_scope(module)
        except WasmScopeError as exc:
            return SandboxResult(
                exit_code=-1, stdout="",
                stderr=(
                    f"[wasm_scope_violation: "
                    f"{exc.import_module}::{exc.import_name}]"
                ),
            )

        return self._instantiate_and_run(module, env_whitelist)

    def _inspect_module_scope(self, module: wasmtime.Module) -> None:
        """ADR-002 Scope Guard.

        Raises :class:`WasmScopeError` on the first import whose module is
        outside :data:`_WASM_ALLOWED_IMPORT_MODULES`.
        """
        for imp in module.imports:
            if imp.module not in _WASM_ALLOWED_IMPORT_MODULES:
                raise WasmScopeError(imp.module, imp.name or "<unnamed>")

    def _instantiate_and_run(
        self, module: wasmtime.Module, env_whitelist: Dict[str, str],
    ) -> SandboxResult:
        """Fuel-metered WASI instantiation + ``_start`` invocation."""
        out_fd, out_path = tempfile.mkstemp(prefix="ail_wasm_out_")
        err_fd, err_path = tempfile.mkstemp(prefix="ail_wasm_err_")
        os.close(out_fd)
        os.close(err_fd)
        try:
            store = wasmtime.Store(self._engine)
            store.set_fuel(_WASM_FUEL_LIMIT)

            wasi = wasmtime.WasiConfig()
            wasi.stdout_file = out_path        # host file — NOT a guest preopen
            wasi.stderr_file = err_path
            if env_whitelist:
                wasi.env = list(env_whitelist.items())
            store.set_wasi(wasi)

            linker = wasmtime.Linker(self._engine)
            linker.define_wasi()
            try:
                instance = linker.instantiate(store, module)
            except wasmtime.WasmtimeError as exc:
                return SandboxResult(
                    exit_code=-1, stdout="",
                    stderr=f"[wasm_instantiate_error: {exc}]",
                )

            try:
                start = instance.exports(store)[_WASM_ENTRYPOINT]
            except KeyError:
                return SandboxResult(
                    exit_code=-1, stdout="",
                    stderr="[wasm_load_error: no _start export]",
                )
            if not isinstance(start, wasmtime.Func):
                return SandboxResult(
                    exit_code=-1, stdout="",
                    stderr="[wasm_load_error: _start is not a function]",
                )

            return self._invoke(start, store, out_path, err_path)
        finally:
            for path in (out_path, err_path):
                try:
                    os.unlink(path)
                except OSError as exc:  # noqa: BLE001 — defensive cleanup
                    logger.warning("Wasm temp cleanup failed: %s", exc)

    def _invoke(
        self,
        start: wasmtime.Func,
        store: wasmtime.Store,
        out_path: str,
        err_path: str,
    ) -> SandboxResult:
        """Call ``_start`` and normalise every exit path to a SandboxResult."""
        exit_code = 0
        try:
            start(store)
        except wasmtime.ExitTrap as exit_trap:
            # Clean WASI termination: libc's `proc_exit` carries the status.
            exit_code = int(getattr(exit_trap, "code", 0))
        except wasmtime.Trap as trap:
            stdout, stderr = self._read_streams(out_path, err_path)
            if self._is_fuel_trap(trap):
                return SandboxResult(
                    exit_code=137, stdout=stdout,
                    stderr="[wasm_fuel_exhausted]",
                )
            return SandboxResult(
                exit_code=-1, stdout=stdout,
                stderr="[wasm_trap: memory_violation]",
            )
        except wasmtime.WasmtimeError as exc:
            stdout, stderr = self._read_streams(out_path, err_path)
            return SandboxResult(
                exit_code=-1, stdout=stdout,
                stderr=f"[wasm_runtime_error: {exc}]",
            )

        stdout, stderr = self._read_streams(out_path, err_path)
        return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    # ── pure helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _is_fuel_trap(trap: wasmtime.Trap) -> bool:
        """True when ``trap`` is an out-of-fuel trap.

        wasmtime surfaces fuel exhaustion as a :class:`wasmtime.Trap` whose
        message contains ``all fuel consumed``. Its internal trap code (11)
        is **not** a member of the Python ``TrapCode`` enum, so reading
        ``trap.trap_code`` raises ``ValueError`` — the message string is the
        only stable signal.
        """
        return "fuel" in (trap.message or "").lower()

    @staticmethod
    def _read_streams(out_path: str, err_path: str) -> Tuple[str, str]:
        """Read + UTF-8-decode the WASI stdout/stderr host temp files."""

        def _read(path: str) -> str:
            try:
                with open(path, "rb") as handle:
                    return handle.read().decode("utf-8", errors="replace")
            except OSError:
                return ""

        return _read(out_path), _read(err_path)


# ── Native Direct interactive tier (persistent PTY) ──────────────────────────


class _DockerPtyBackend(_PtyBackend):
    """Persistent ``sh`` inside the sandbox daemon container over an exec socket.

    The exec is created with ``tty=True`` so the attached socket carries a raw
    stream (no Docker 8-byte stream-multiplexing header) and the container shell
    has real line discipline. Blocking ``recv`` runs in the session's reader
    thread, exactly like the host PTY master read.
    """

    def __init__(
        self,
        client: Any,
        container_id: str,
        cwd: str,
        env: Dict[str, str],
        argv: List[str],
    ) -> None:
        self._api = client.api
        created = self._api.exec_create(
            container_id,
            argv,
            workdir=cwd or _CONTAINER_WORKDIR,
            environment=dict(env),
            stdin=True,
            tty=True,
        )
        self._exec_id = created["Id"]
        sock = self._api.exec_start(self._exec_id, socket=True, tty=True)
        # docker-py wraps the raw socket; the OS socket is exposed at ``_sock``.
        self._sock = getattr(sock, "_sock", sock)
        try:
            self._sock.setblocking(True)
        except OSError:
            pass

    @property
    def pid(self) -> Optional[int]:
        return None

    def read(self, size: int) -> bytes:
        try:
            return bytes(self._sock.recv(size))
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def send_interrupt(self) -> None:
        try:
            self._sock.sendall(b"\x03")
        except OSError:
            pass

    def terminate_tree(self) -> None:
        # Closing the exec socket ends the in-container shell; the container
        # itself is reaped by DockerSandboxAdapter.shutdown.
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            info = self._api.exec_inspect(self._exec_id)
            code = info.get("ExitCode")
            return int(code) if code is not None else None
        except Exception:  # noqa: BLE001 — inspect best-effort during teardown
            return None


async def _collect_stream(session: SandboxSession, sink: List[bytes]) -> None:
    """Drain a session's output stream into ``sink`` until it closes."""
    async for chunk in session.stream():
        sink.append(chunk)


class NativeDirectSandboxAdapter(SandboxAdapter):
    """Host-native tier with a persistent interactive shell (no per-command HITL).

    Unlike :class:`NativeHITLSandboxAdapter` — which suspends every call on a
    human approval and returns a single buffered result — this tier owns a
    long-lived PTY: output streams incrementally, ``stdin`` is writable, and the
    process tree can be interrupted or killed. Governance (allowlist plus
    session-level approval) is applied by the dispatcher above this layer, not
    per command here. Defined for the session machinery; the startup resolver
    does not yet select it.
    """

    supports_sessions = True

    async def open_session(
        self,
        *,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> SandboxSession:
        del session_id  # session identity is the dispatcher's concern
        session = _PtySession(
            cwd=cwd,
            env=dict(env_whitelist),
            pre_spawn_guard=pre_spawn_guard,
        )
        await session.start()
        return session

    def get_sync_surface(self, cwd: str) -> "SyncSurface":
        """Return a LocalFsSyncSurface rooted at the session's cwd."""
        from core.workspace_sync import LocalFsSyncSurface
        return LocalFsSyncSurface(cwd)

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        """One-shot convenience over a transient session: open → run → drain → close."""
        session = await self.open_session(
            cwd=cwd, env_whitelist=env_whitelist, session_id=session_id,
        )
        chunks: List[bytes] = []
        collector = asyncio.ensure_future(_collect_stream(session, chunks))
        try:
            exit_code = await session.run(command, timeout_s=timeout_s)
        except asyncio.TimeoutError:
            await session.kill()
            await asyncio.gather(collector, return_exceptions=True)
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[native_direct_timeout]",
            )
        await session.close()
        await asyncio.gather(collector, return_exceptions=True)
        body = b"".join(chunks).decode("utf-8", errors="replace")
        return SandboxResult(exit_code=exit_code, stdout=body, stderr="")


# ── Phase 6.1.4 — startup tier resolution ────────────────────────────────────

ACTIVE_TIER: Optional[Literal["DOCKER", "WASM", "NATIVE_HITL"]] = None
ACTIVE_ADAPTER: Optional[SandboxAdapter] = None

_DOCKER_PROBE_TIMEOUT_S: float = 2.0


async def resolve_default_adapter() -> None:
    """Probe the three sandbox tiers in degradation order; bind the globals.

    Order: Docker (default) → Wasm (degraded, pure-compute) → NativeHITL
    (last-resort host exec). Called once from the FastAPI lifespan at startup.
    Idempotent — safe to re-invoke. Never raises: a total failure still binds
    the NativeHITL tier.
    """
    global ACTIVE_TIER, ACTIVE_ADAPTER

    # Tier 1 — Docker (daemon reachable within 2 s).
    try:
        client = docker.from_env()
        await asyncio.wait_for(
            asyncio.to_thread(client.ping), timeout=_DOCKER_PROBE_TIMEOUT_S,
        )
        ACTIVE_TIER = "DOCKER"
        ACTIVE_ADAPTER = DockerSandboxAdapter()
        logger.info("Sandbox tier resolved: DOCKER (daemon reachable).")
        return
    except Exception as exc:  # noqa: BLE001 — any probe failure → degrade
        logger.warning("Docker probe failed (%s) — falling back to Wasm.", exc)

    # Tier 2 — Wasm (constructing the adapter exercises the wasmtime runtime).
    try:
        wasm_adapter = WasmSandboxAdapter()
        ACTIVE_TIER = "WASM"
        ACTIVE_ADAPTER = wasm_adapter
        logger.warning(
            "Sandbox tier resolved: WASM (DEGRADED — Docker unavailable; "
            "pure-compute only).",
        )
        return
    except Exception as exc:  # noqa: BLE001 — wasmtime broken → degrade
        logger.warning(
            "Wasm probe failed (%s) — falling back to NativeHITL.", exc,
        )

    # Tier 3 — NativeHITL (last-resort host exec, human-gated).
    ACTIVE_TIER = "NATIVE_HITL"
    ACTIVE_ADAPTER = NativeHITLSandboxAdapter()
    logger.warning(
        "Sandbox tier resolved: NATIVE_HITL (DEGRADED — last-resort "
        "host execution, human-in-the-loop gated).",
    )


def get_active_tier() -> Optional[Literal["DOCKER", "WASM", "NATIVE_HITL"]]:
    """Stable accessor for the resolved tier.

    Consumers MUST call this rather than a ``from core.sandbox import
    ACTIVE_TIER`` binding — the resolver reassigns the global at startup, so
    a from-import would capture a stale ``None``. Phase 6.1.4 defers the
    frontend ``sandbox_tier`` badge; this getter is the seam a later phase
    uses to read the tier without import-order coupling.
    """
    return ACTIVE_TIER


def get_active_adapter() -> Optional[SandboxAdapter]:
    """Stable accessor for the resolved adapter instance.

    Consumers (e.g. ``tools/execution_tools.py``) MUST call this rather than a
    ``from core.sandbox import ACTIVE_ADAPTER`` binding — the resolver
    reassigns the global at startup, so a from-import would capture a stale
    ``None``.
    """
    return ACTIVE_ADAPTER


# ── Zero-config image pull (Phase 7.9.B.8) ───────────────────────────────────


async def pull_sandbox_image() -> None:
    """Pull the pre-built sandbox image from the public registry and retag it
    to the local adapter tag so :class:`DockerSandboxAdapter` finds it without
    a build.

    Blocking SDK calls are offloaded to a worker thread. Propagates
    ``docker.errors.*`` / connection errors to the caller for structured
    handling (the ``api.runtime`` endpoint maps them to client error codes).
    """
    client = await asyncio.to_thread(docker.from_env)
    await asyncio.to_thread(_pull_and_tag_sync, client)


def _pull_and_tag_sync(client: Any) -> None:
    """Blocking pull + local retag (always called via asyncio.to_thread)."""
    image = client.images.pull(_SANDBOX_REMOTE_REPO, tag=_SANDBOX_REMOTE_TAG)
    # Retag to the local tag the adapter's _image_exists() / run() expect.
    repo, _, tag = _SANDBOX_IMAGE_TAG.partition(":")
    image.tag(repo, tag=tag or "latest")
