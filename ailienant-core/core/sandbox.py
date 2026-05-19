# ailienant-core/core/sandbox.py
"""Phase 6.1.1 — Pluggable Sandbox Adapter (base ABC + Docker concrete).

Implements the host-isolation primitive defined in
``docs/PHASE_6_BLUEPRINT.md §2``. Today every EXECUTE-tier tool call hits
``asyncio.create_subprocess_shell`` against the host with full parent
privileges — Phase 6.1.1 lands the adapter contract and the Docker concrete
that the Phase 6.1.4 resolver will pick as the default tier on machines where
the Docker daemon is reachable.

Out of scope for 6.1.1 (deferred to later sub-tasks of Phase 6.1):
    * ``NativeHITLSandboxAdapter`` (6.1.2)
    * ``WasmSandboxAdapter`` (6.1.3)
    * ``resolve_default_adapter`` startup probe + ``ACTIVE_ADAPTER`` global (6.1.4)
    * Dispatch swap in ``tools/execution_tools.py`` (6.2)

All synchronous ``docker`` SDK calls are wrapped in :func:`asyncio.to_thread` to
protect the FastAPI event loop — same discipline as :mod:`core.janitor`.
Timeouts are enforced **inside** the container by the GNU ``timeout`` coreutils
(SIGTERM then SIGKILL), not via :func:`asyncio.wait_for`, to avoid leaking
``ThreadPoolExecutor`` workers on long-running or runaway commands.
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
        * ``NativeHITLSandboxAdapter`` (Phase 6.1.2)
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
    ) -> SandboxResult:
        """Run ``command`` inside the adapter's isolation envelope.

        ``env_whitelist`` is the **only** environment dictionary the command
        sees — host environment (including API keys) MUST NOT leak through.
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
    ) -> SandboxResult:
        """Dispatch ``command`` to the sandbox container.

        Timeout is enforced **inside** the container by the GNU ``timeout``
        coreutils; the kernel SIGTERMs (then SIGKILLs after 1 s grace) the
        process group when the deadline expires and ``exec_run`` returns
        naturally with exit code 124, freeing the Python worker thread
        instantly.
        """
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
