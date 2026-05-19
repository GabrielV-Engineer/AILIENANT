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

Out of scope for this module (deferred to later sub-tasks of Phase 6):
    * ``WasmSandboxAdapter`` (Phase 6.1.3)
    * ``resolve_default_adapter`` startup probe + ``ACTIVE_ADAPTER`` global (6.1.4)
    * Dispatch swap in ``tools/execution_tools.py`` (Phase 6.2)

All synchronous ``docker`` SDK calls are wrapped in :func:`asyncio.to_thread` to
protect the FastAPI event loop — same discipline as :mod:`core.janitor`.
Docker-tier timeouts are enforced **inside** the container by the GNU
``timeout`` coreutils (SIGTERM then SIGKILL); the NativeHITL tier enforces
timeouts host-side via :func:`asyncio.wait_for` and reaps the OS process to
prevent zombie accumulation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from abc import ABC, abstractmethod
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

import docker  # type: ignore[import-untyped]
from pydantic import BaseModel

logger = logging.getLogger("AILIENANT_SANDBOX")

# ── Module constants ─────────────────────────────────────────────────────────

_SANDBOX_IMAGE_TAG: str = "ailienant-sandbox:latest"
_SANDBOX_CONTAINER_NAME: str = "ailienant-sandbox-daemon"
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
        * ``WasmSandboxAdapter`` (Phase 6.1.3)
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

    def __init__(self, *, host_workspace: Optional[str] = None) -> None:
        self._client: Optional[Any] = None
        self._container: Optional[Any] = None
        self._image_id: Optional[str] = None
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._host_workspace: str = host_workspace or os.getcwd()

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
