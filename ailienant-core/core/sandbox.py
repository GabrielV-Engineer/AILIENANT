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
import hashlib
import logging
import math
import os
import shlex
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Protocol, Tuple, Type

if TYPE_CHECKING:
    from core.workspace_sync import SyncSurface

import docker
import docker.errors  # explicit submodule import so the type checker resolves docker.errors.*
import requests
import wasmtime
from pydantic import BaseModel

from core.pty_session import (
    PreSpawnGuard,
    SandboxSession,
    SandboxSessionError,
    _PtyBackend,
    _PtySession,
)
from shared.config import (
    DOCKER_OP_TIMEOUT_S,
    SANDBOX_IDLE_TTL_S,
    SANDBOX_LEASE_WAIT_S,
    SANDBOX_MAX_CONTAINERS,
    SANDBOX_MEM_LIMIT,
    SANDBOX_PIDS_LIMIT,
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


# ── Daemon-hang containment (DEBT-100) ───────────────────────────────────────
#
# Every blocking Docker SDK call funnels through :func:`_docker_call`, which
# (a) dispatches to a *dedicated, bounded* worker pool — never
# ``asyncio.to_thread``'s shared default executor, so a stalled daemon can
# never starve unrelated ``to_thread`` consumers elsewhere in the process
# (janitor, PPR, indexer, blast-radius) — and (b) is guarded by a small
# circuit breaker so a sustained hang stops dispatching new calls entirely
# rather than burning one worker thread per retry.
#
# The primary defense is narrower than the executor alone suggests: every
# Docker client used here is constructed with an explicit socket-level
# ``timeout`` (verified against docker-py 7.1.0's ``APIClient(timeout=...)``),
# so an unresponsive daemon surfaces as ``requests.exceptions.ReadTimeout`` on
# the worker thread, which then returns to the pool in O(1) — no orphaned
# thread. The one call this cannot bound is the *hijacked exec socket* behind
# an interactive PTY session (``exec_start(socket=True)``): that is a
# deliberately blocking raw-socket read with no HTTP timeout underneath it.
# The dedicated pool + breaker exist to contain exactly that residual case
# (see DEBT entry logged alongside this phase).


class SandboxDaemonTimeout(Exception):
    """The Docker daemon itself is unresponsive — not a hung in-container
    command (already bounded by the GNU ``timeout`` wrapper), but the daemon
    failing to answer an SDK call within its budget. Callers translate this
    into a bracketed degrade sentinel; it must never propagate past
    :meth:`SandboxAdapter.execute` / :meth:`SandboxAdapter.open_session`.
    """


class SandboxResourceExhausted(Exception):
    """The container pool is at capacity, no lease is idle, and sharing would
    require crossing mount roots. Refused outright rather than degraded: a
    shared container mounted at a *different* project's root would silently
    execute the caller's command against the wrong files (context corruption),
    which is a correctness defect, not merely a lost isolation guarantee.
    """


_DOCKER_EXECUTOR_LOCK = threading.Lock()
_docker_executor: Optional[ThreadPoolExecutor] = None


def _get_docker_executor() -> ThreadPoolExecutor:
    """Lazily build the module-wide bounded Docker worker pool.

    Sized off the container pool cap so a fully-leased pool's worth of
    concurrent operations never queues behind too few threads. Distinct from
    (and never shared with) the interpreter's default ``asyncio.to_thread``
    executor, which every other subsystem in the process still depends on.
    """
    global _docker_executor
    if _docker_executor is None:
        with _DOCKER_EXECUTOR_LOCK:
            if _docker_executor is None:
                _docker_executor = ThreadPoolExecutor(
                    max_workers=max(4, 2 * SANDBOX_MAX_CONTAINERS),
                    thread_name_prefix="ail-docker",
                )
    return _docker_executor


# Pure transport/connectivity faults only — NOT `docker.errors.APIError` or its
# `NotFound`/`ImageNotFound` subclasses, which are ordinary application-level
# responses from a *live* daemon that existing call sites already handle as
# control flow (e.g. `_image_exists`). Conflating those with a daemon hang
# would misfire the breaker on every expected 404 and break that control flow.
#
# `docker.errors.DockerException` is handled separately in `_docker_call`
# rather than added here: it is the ANCESTOR of `APIError` (isinstance would
# swallow `NotFound`/`ImageNotFound` too), yet `docker.from_env()` itself
# raises a bare `DockerException` — never an `APIError` — when there is no
# daemon to connect to at all (verified: "Error while fetching server API
# version" on a machine with no Docker installed). That construction-time
# failure IS a daemon fault and must degrade, not crash the caller.
_DAEMON_FAULT_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


class _DaemonBreaker:
    """Minimal three-state circuit breaker scoped to Docker daemon reachability.

    Not a reuse of ``brain/nodes/circuit_breaker.py`` — that module is agent
    retry semantics over LLM turns, not I/O health. Opening this breaker is
    what actually prevents thread exhaustion under a sustained hang: a socket
    timeout alone still burns one ``ail-docker`` thread per attempt, so once
    the daemon is known-bad the breaker stops dispatching entirely until a
    cheap probe says otherwise.
    """

    def __init__(self, *, fail_threshold: int = 2, cooldown_s: float = 60.0) -> None:
        self._fail_threshold = fail_threshold
        self._cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """Closed/half-open → False (a call may proceed); Open → True."""
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown_s:
            return False  # cooldown elapsed: let the next call through as a probe
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._fail_threshold:
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Test-isolation / explicit re-arm hook."""
        self._consecutive_failures = 0
        self._opened_at = None


_daemon_breaker = _DaemonBreaker()


def reset_daemon_breaker() -> None:
    """Drop the module-level breaker state (test isolation)."""
    _daemon_breaker.reset()


async def _docker_call(fn: Any, *args: Any, timeout_s: float, op: str, **kwargs: Any) -> Any:
    """Run a blocking Docker SDK call on the bounded ``ail-docker`` pool.

    Breaker-guarded up front so a known-bad daemon fails instantly without
    dispatching (and thus without risking) a new thread. Raises
    :class:`SandboxDaemonTimeout` on a timeout, a transport/connectivity fault,
    or a daemon that could not be reached at all (a bare
    ``docker.errors.DockerException`` — e.g. ``docker.from_env()`` itself
    failing when no daemon/socket exists on the host). Every other exception —
    including ``docker.errors.APIError``/``NotFound``/``ImageNotFound``, which
    are legitimate application-level responses from a *live* daemon — is a
    ``DockerException`` subclass too but propagates unchanged, so existing
    control-flow call sites (e.g. ``_image_exists``) are unaffected.
    """
    if _daemon_breaker.is_open:
        raise SandboxDaemonTimeout(f"circuit open — refusing Docker op {op!r}")

    loop = asyncio.get_running_loop()
    executor = _get_docker_executor()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: fn(*args, **kwargs)),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        _daemon_breaker.record_failure()
        raise SandboxDaemonTimeout(f"Docker op {op!r} exceeded {timeout_s}s") from exc
    except _DAEMON_FAULT_EXCEPTIONS as exc:
        _daemon_breaker.record_failure()
        raise SandboxDaemonTimeout(f"Docker op {op!r} failed: {exc}") from exc
    except docker.errors.DockerException as exc:
        if isinstance(exc, docker.errors.APIError):
            raise  # a live daemon's application-level response — control flow, not a fault
        # A bare DockerException (not an APIError) means the daemon could not
        # be reached at all — e.g. docker.from_env() itself fails when no
        # daemon/socket exists on the host.
        _daemon_breaker.record_failure()
        raise SandboxDaemonTimeout(f"Docker op {op!r} failed: {exc}") from exc
    _daemon_breaker.record_success()
    return result


async def docker_call(fn: Any, *args: Any, timeout_s: float, op: str, **kwargs: Any) -> Any:
    """Public alias of :func:`_docker_call`.

    Cross-module callers (``api/runtime.py``'s daemon-reachability probes,
    which the RuntimePanel polls continuously) route their own blocking Docker
    SDK calls through this so the whole process shares one bounded pool and one
    breaker, rather than each caller risking the shared default executor.
    """
    return await _docker_call(fn, *args, timeout_s=timeout_s, op=op, **kwargs)


def is_daemon_breaker_open() -> bool:
    """Whether the daemon circuit breaker is currently open (degraded state)."""
    return _daemon_breaker.is_open


# ── Exec-only timeout-bucketed clients ───────────────────────────────────────
#
# Verified against docker-py 7.1.0: neither ``exec_create`` nor ``exec_start``
# accept a per-call timeout, and ``exec_run`` blocks until the command
# completes — so a single shared short-timeout client would sever a
# legitimate long-running command. Each one-shot ``execute()`` call instead
# resolves a client scoped to its own budget, rounded up to a coarse bucket and
# LRU-cached so a highly variable ``timeout_s`` cannot create unbounded clients
# (each carries its own connection pool).

_EXEC_CLIENT_BUCKET_S: float = 30.0
_EXEC_CLIENT_CACHE_CAP: int = 8
_EXEC_TIMEOUT_OUTER_GRACE_S: float = 5.0  # outer net above the in-container `timeout`

_exec_client_cache: "OrderedDict[float, Any]" = OrderedDict()
_exec_client_cache_lock = threading.Lock()


def _exec_timeout_bucket(timeout_s: float) -> float:
    return _EXEC_CLIENT_BUCKET_S * math.ceil(max(timeout_s, 1.0) / _EXEC_CLIENT_BUCKET_S)


async def _get_exec_client(timeout_s: float) -> Any:
    """LRU-cached, timeout-scoped client for one-shot exec calls.

    Construction itself is a blocking daemon round-trip (docker-py resolves
    the server API version at client build time), so a cache miss is
    dispatched through :func:`_docker_call` at the *short* op budget — building
    a client should never itself hang for as long as the exec it will run.
    """
    bucket = _exec_timeout_bucket(timeout_s)
    with _exec_client_cache_lock:
        cached = _exec_client_cache.get(bucket)
        if cached is not None:
            _exec_client_cache.move_to_end(bucket)
            return cached
    client = await _docker_call(
        docker.from_env, timeout=bucket, timeout_s=DOCKER_OP_TIMEOUT_S, op="from_env_exec_bucket",
    )
    with _exec_client_cache_lock:
        _exec_client_cache[bucket] = client
        _exec_client_cache.move_to_end(bucket)
        while len(_exec_client_cache) > _EXEC_CLIENT_CACHE_CAP:
            _exec_client_cache.popitem(last=False)
    return client


def _run_exec_sync(
    client: Any,
    container_id: str,
    wrapped_command: str,
    container_cwd: str,
    env_whitelist: Dict[str, str],
) -> Tuple[int, Any]:
    """Mirror ``Container.exec_run(demux=True)`` against a timeout-scoped client.

    Reimplemented at the low-level ``APIClient`` layer (``exec_create`` /
    ``exec_start`` / ``exec_inspect``) because the high-level ``exec_run``
    convenience method is bound to the container's *own* client instance,
    which carries the process-wide lifecycle timeout rather than this call's
    exec-specific budget.
    """
    api = client.api
    created = api.exec_create(
        container_id,
        wrapped_command,
        workdir=container_cwd,
        environment=dict(env_whitelist),
        stdout=True,
        stderr=True,
        tty=False,
    )
    output = api.exec_start(created["Id"], demux=True)
    info = api.exec_inspect(created["Id"])
    return int(info.get("ExitCode") or 0), output


# ── Session→workspace-root DI seam (keys the per-session container pool) ────

_session_workspace_resolver: Optional[Callable[[str], str]] = None


def set_session_workspace_resolver(fn: Optional[Callable[[str], str]]) -> None:
    """Inject (or clear) the session-id → workspace-root lookup.

    Mirrors the :func:`set_trusted_bridge` precedent: ``core`` never imports
    the transport layer that owns the session registry (``main.py``'s
    ``_session_workspace_root``, keyed by the same ``client_id == x_task_id ==
    session_id`` identity the rest of this file already assumes) — the lookup
    is pushed down from the composition root instead. Left uninjected (the
    default, and what every unit test gets), every lease falls back to the
    adapter's own ``host_workspace`` — today's single-mount behavior — so the
    seam is safe to leave unwired.
    """
    global _session_workspace_resolver
    _session_workspace_resolver = fn


def _lease_key(mount_root: str, session_id: Optional[str]) -> Tuple[str, str]:
    return (os.path.abspath(mount_root), session_id or "__shared__")


def _lease_container_name(key: Tuple[str, str]) -> str:
    digest = hashlib.sha1("::".join(key).encode("utf-8")).hexdigest()[:12]
    return f"ailienant-sandbox-{digest}"


class _ContainerLease:
    """One pooled container, its mount root, and its live-borrower count."""

    __slots__ = ("container", "mount_root", "refcount", "last_used")

    def __init__(self, container: Any, mount_root: str, refcount: int = 1) -> None:
        self.container = container
        self.mount_root = mount_root
        self.refcount = refcount
        self.last_used: float = time.monotonic()


class _ContainerPool:
    """Bounded per-``(mount root, session)`` Docker container leases.

    Replaces the pre-12.6 adapter's single shared ``self._container``:
    concurrent sessions against different projects — or the same one — get
    their own container instead of contending for one CPU/memory envelope and
    one ``/work`` tmpfs. Because a lease's mount root travels with it, a
    session against a different project can never silently fall back onto
    another project's ``/workspace`` (the wrong-mount defect this pool also
    removes).

    All structural mutation (create/evict/share) happens under one
    ``asyncio.Lock``, matching the single-lock discipline the pre-existing
    ``_lifecycle_lock`` already used in this file — pool operations are
    already meant to serialize, and every blocking step under the lock is
    itself breaker-guarded and timeout-bounded via :func:`_docker_call`.
    """

    def __init__(self, adapter: "DockerSandboxAdapter") -> None:
        self._adapter = adapter
        self._leases: "OrderedDict[Tuple[str, str], _ContainerLease]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

    def peek(self, mount_root: str, session_id: Optional[str]) -> Optional[_ContainerLease]:
        """Non-blocking read of an already-established lease (no acquire)."""
        return self._leases.get(_lease_key(mount_root, session_id))

    async def acquire(self, *, mount_root: str, session_id: Optional[str]) -> _ContainerLease:
        key = _lease_key(mount_root, session_id)
        abs_root = key[0]
        async with self._lock:
            lease = self._leases.get(key)
            if lease is not None:
                if await self._adapter._revalidate(lease.container):
                    lease.refcount += 1
                    lease.last_used = time.monotonic()
                    self._leases.move_to_end(key)
                    return lease
                del self._leases[key]  # vanished underneath us — recreate below

            await self._reap_expired_idle_locked()

            if len(self._leases) < SANDBOX_MAX_CONTAINERS:
                return await self._create_locked(key, abs_root)
            if await self._evict_one_idle_locked():
                return await self._create_locked(key, abs_root)

            try:
                await asyncio.wait_for(
                    self._condition.wait_for(self._has_capacity_or_idle_locked),
                    timeout=SANDBOX_LEASE_WAIT_S,
                )
            except asyncio.TimeoutError:
                return self._share_or_raise_locked(abs_root)

            if len(self._leases) < SANDBOX_MAX_CONTAINERS:
                return await self._create_locked(key, abs_root)
            if await self._evict_one_idle_locked():
                return await self._create_locked(key, abs_root)
            return self._share_or_raise_locked(abs_root)  # woke without real capacity

    async def release(self, lease: _ContainerLease) -> None:
        async with self._lock:
            lease.refcount = max(0, lease.refcount - 1)
            lease.last_used = time.monotonic()
            self._condition.notify_all()

    async def drain(self) -> None:
        """Tear down every lease — called from the FastAPI lifespan shutdown."""
        async with self._lock:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            await self._adapter._teardown_container(lease.container, reason="drained")

    # ── lock-held helpers ────────────────────────────────────────────────────

    def _has_capacity_or_idle_locked(self) -> bool:
        if len(self._leases) < SANDBOX_MAX_CONTAINERS:
            return True
        return any(lease.refcount == 0 for lease in self._leases.values())

    async def _create_locked(self, key: Tuple[str, str], abs_root: str) -> _ContainerLease:
        container = await self._adapter._create_lease_container(key, abs_root)
        lease = _ContainerLease(container=container, mount_root=abs_root)
        self._leases[key] = lease
        return lease

    async def _evict_one_idle_locked(self) -> bool:
        idle = [(k, l) for k, l in self._leases.items() if l.refcount == 0]
        if not idle:
            return False
        idle.sort(key=lambda kv: kv[1].last_used)
        key, lease = idle[0]
        del self._leases[key]
        await self._adapter._teardown_container(lease.container, reason="evicted")
        return True

    async def _reap_expired_idle_locked(self) -> None:
        now = time.monotonic()
        expired = [
            (k, l) for k, l in self._leases.items()
            if l.refcount == 0 and (now - l.last_used) >= SANDBOX_IDLE_TTL_S
        ]
        for key, lease in expired:
            del self._leases[key]
            await self._adapter._teardown_container(lease.container, reason="idle_ttl_reaped")

    def _share_or_raise_locked(self, mount_root: str) -> _ContainerLease:
        """Pool exhausted: share only a lease mounted at the SAME root.

        Sharing across mount roots is refused unconditionally — the borrowed
        container's ``cwd`` translation would silently resolve against the
        wrong project's ``/workspace``, corrupting the caller's execution
        context rather than merely losing CPU/RAM isolation.
        """
        candidates = [lease for lease in self._leases.values() if lease.mount_root == mount_root]
        if not candidates:
            raise SandboxResourceExhausted(
                f"container pool exhausted (cap={SANDBOX_MAX_CONTAINERS}) and no "
                f"lease mounted at {mount_root!r} to share — refusing to cross "
                f"mount roots, which would execute against the wrong project."
            )
        candidates.sort(key=lambda lease: lease.last_used)
        lease = candidates[0]
        lease.refcount += 1
        lease.last_used = time.monotonic()
        logger.warning(
            "Sandbox pool exhausted (cap=%d) — sharing container mounted at %s "
            "(refcount now %d). CPU/RAM isolation degraded; disk/mount safety "
            "is preserved because the share is same-root only.",
            SANDBOX_MAX_CONTAINERS, mount_root, lease.refcount,
        )
        self._adapter._emit_lifecycle("shared_degraded", lease.container)
        return lease


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

    execution_source: str = "unknown"
    """Wire identity for the Glass-Box Timeline's execution-detail channel
    (``api.ws_contracts.ExecutionSource``). Overridden per concrete tier so
    ``core/exec_log.py`` can label a command's execution envelope without
    importing any adapter class — read via
    ``getattr(adapter, "execution_source", "unknown")``, so a bare test double
    conforming only to the narrower ``_ExecAdapter`` protocol still resolves
    safely to the "unknown" default.
    """

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

    def get_sync_surface(self, cwd: str, session_id: Optional[str] = None) -> "SyncSurface":
        """Return the writable SyncSurface for this adapter.

        Session-capable tiers (Docker, NativeDirect) override this. Tiers
        without an interactive work surface (Wasm, HITL) inherit the default
        which raises, consistent with the open_session pattern.

        ``session_id`` is additive (Phase 12.6): the Docker tier's pool keys a
        container by session, so the surface must be resolved against the
        *same* lease :meth:`open_session` established for this session, not
        an adapter-wide container. Tiers with no per-session lease concept
        accept and ignore it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose a sync surface."
        )


# ── Docker concrete adapter ──────────────────────────────────────────────────


def _owner_port() -> str:
    """The port this process's host-discovery file advertises, if any.

    Read directly from the environment (mirroring ``main.py``'s own
    ``_publish_host_discovery`` resolution) so the container-label contract
    needs no import of the transport layer. Empty when unset — a manual,
    non-extension-spawned backend — in which case the sweep in
    :func:`sweep_orphaned_containers` treats the container as unattributed
    rather than guessing a port.
    """
    return os.environ.get("AILIENANT_API_PORT", "").strip()


class DockerSandboxAdapter(SandboxAdapter):
    """Bounded pool of ``ailienant-sandbox-*`` containers, one per live session.

    Security profile per container (locked to ``PHASE_6_BLUEPRINT.md §2.2``):

    * ``--read-only`` rootfs
    * ``--network none``
    * this lease's mount root bind-mounted at ``/workspace`` read-only
    * ``tmpfs`` at ``/work`` (512 MB, ``nosuid``, ``nodev``)
    * ``mem_limit`` / ``pids_limit`` ceilings (Phase 12.6 — noisy-neighbor bound)
    * Environment filtered to the per-call ``env_whitelist``

    Containers are leased per ``(mount root, session)`` via :class:`_ContainerPool`
    rather than shared as a single process-lifetime container — see that
    class's docstring for the isolation and wrong-mount rationale. Every
    blocking Docker SDK call routes through :func:`_docker_call`, which is
    both timeout-bounded and breaker-guarded (Phase 12.6 — DEBT-100).
    """

    execution_source = "docker"

    supports_sessions = True

    def __init__(self, *, host_workspace: Optional[str] = None) -> None:
        self._client: Optional[Any] = None
        self._build_client: Optional[Any] = None
        self._image_ready: bool = False
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._image_lock: asyncio.Lock = asyncio.Lock()
        self._host_workspace: str = host_workspace or os.getcwd()
        self._pool: _ContainerPool = _ContainerPool(self)

    @property
    def host_workspace(self) -> str:
        """The adapter-default mount root — the ``__shared__`` lease's root.

        The single authority for callers with no live session (the untrusted
        benchmark oracle, hook execution) that must know the mount point
        *before* any lease exists, e.g. to materialize a file the container
        will later read. A session-scoped lease may mount a *different* root
        (see :func:`set_session_workspace_resolver`); this property never
        reflects that — it names only the shared/default mount.
        """
        return self._host_workspace

    def pooled_container_count(self) -> int:
        """Non-blocking snapshot of currently-leased containers.

        A plain in-process dict read — no Docker I/O — so ``api/runtime.py``'s
        continuously-polled status endpoint can report it without adding load
        to (or risking a hang against) the daemon on every poll.
        """
        return len(self._pool._leases)  # noqa: SLF001 — same-module collaborator

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
        """Dispatch ``command`` inside this session's leased container.

        Timeout is enforced **inside** the container by the GNU ``timeout``
        coreutils; the kernel SIGTERMs (then SIGKILLs after 1 s grace) the
        process group when the deadline expires and exec returns naturally
        with exit code 124. A daemon that is itself unresponsive — as opposed
        to a hung in-container command — degrades to a bracketed sentinel
        rather than raising; the pool's own exhaustion degrades the same way.
        """
        mount_root = self._resolve_mount_root(session_id)
        try:
            lease = await self._pool.acquire(mount_root=mount_root, session_id=session_id)
        except SandboxDaemonTimeout as exc:
            logger.error("Sandbox daemon unavailable acquiring a lease: %s", exc, exc_info=True)
            return SandboxResult(exit_code=-1, stdout="", stderr="[sandbox_daemon_unavailable]")
        except SandboxResourceExhausted as exc:
            logger.warning("Sandbox pool exhausted: %s", exc)
            return SandboxResult(exit_code=-1, stdout="", stderr="[sandbox_pool_exhausted]")

        try:
            container_cwd = self._translate_cwd(cwd, lease.mount_root)
            wrapped = (
                f"timeout --foreground -k 1 {int(timeout_s)}s "
                f"sh -c {shlex.quote(command)}"
            )
            exec_budget = timeout_s + _EXEC_TIMEOUT_OUTER_GRACE_S
            try:
                exec_client = await _get_exec_client(exec_budget)
                exit_code, output = await _docker_call(
                    _run_exec_sync, exec_client, lease.container.id, wrapped, container_cwd,
                    env_whitelist, timeout_s=exec_budget, op="exec_run",
                )
            except SandboxDaemonTimeout as exc:
                logger.error("Sandbox daemon unavailable during exec: %s", exc, exc_info=True)
                return SandboxResult(exit_code=-1, stdout="", stderr="[sandbox_daemon_unavailable]")

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
            elif exit_code == 137:
                # `timeout` returns 124 whenever IT kills the child, even after
                # escalating to SIGKILL — so 137 here means something else ended
                # it, dominantly the cgroup OOM killer given `mem_limit` is in
                # force. Name the knob so the model can react instead of seeing
                # a bare non-zero exit.
                oom_note = (
                    f"[sandbox_oom] process was killed (exit 137) — likely exceeded "
                    f"the {SANDBOX_MEM_LIMIT} memory ceiling (AILIENANT_SANDBOX_MEM_LIMIT)"
                )
                stderr = f"{oom_note}\n{stderr}" if stderr else oom_note

            return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
        finally:
            await self._pool.release(lease)

    async def open_session(
        self,
        *,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
    ) -> SandboxSession:
        """Open a persistent ``sh`` inside this session's leased container.

        The exec is created with a TTY so the stream is raw (no 8-byte demux
        header) and line discipline is real, matching the host PTY model. The
        lease is held for the session's whole life and released exactly once
        when the backend closes (:class:`_DockerPtyBackend`'s ``on_close``) —
        without that release the container could never become idle-evictable.
        """
        mount_root = self._resolve_mount_root(session_id)
        try:
            lease = await self._pool.acquire(mount_root=mount_root, session_id=session_id)
        except SandboxDaemonTimeout as exc:
            raise SandboxSessionError(
                "Sandbox daemon unavailable — cannot open a session."
            ) from exc
        except SandboxResourceExhausted as exc:
            raise SandboxSessionError(str(exc)) from exc

        client = await self._get_client()
        container_id = lease.container.id
        container_cwd = self._translate_cwd(cwd, lease.mount_root)
        loop = asyncio.get_running_loop()
        released = False

        def _release_once() -> None:
            nonlocal released
            if released:
                return
            released = True
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._pool.release(lease)))

        def _factory(
            argv: List[str], _cwd: str, env: Dict[str, str], _marker: bytes,
        ) -> _PtyBackend:
            return _DockerPtyBackend(
                client, container_id, container_cwd, env, argv, on_close=_release_once,
            )

        session = _PtySession(
            cwd=container_cwd,
            env=dict(env_whitelist),
            shell_kind="posix",
            pre_spawn_guard=pre_spawn_guard,
            backend_factory=_factory,
        )
        await session.start()
        return session

    def get_sync_surface(self, cwd: str, session_id: Optional[str] = None) -> "SyncSurface":
        """Return a DockerSyncSurface targeting /work of THIS session's lease."""
        mount_root = self._resolve_mount_root(session_id)
        lease = self._pool.peek(mount_root, session_id)
        if lease is None:
            raise RuntimeError(
                "DockerSandboxAdapter: no active container lease for this session; "
                "call open_session() first."
            )
        from core.workspace_sync import DockerSyncSurface
        return DockerSyncSurface(lease.container, _CONTAINER_TMPFS_PATH)

    async def shutdown(self) -> None:
        """Drain every pooled container and close the Docker clients.

        Idempotent — safe to call from the FastAPI lifespan shutdown whether
        any container was ever started or not.
        """
        await self._pool.drain()
        for attr in ("_client", "_build_client"):
            client = getattr(self, attr)
            if client is not None:
                try:
                    await _docker_call(client.close, timeout_s=DOCKER_OP_TIMEOUT_S, op=f"close_{attr}")
                except Exception as exc:  # noqa: BLE001 — defensive cleanup, never blocks shutdown
                    logger.warning("Docker client close failed (%s): %s", attr, exc)
                setattr(self, attr, None)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def _get_client(self) -> Any:
        """Lazily build the shared short-timeout client used for lifecycle ops."""
        if self._client is not None:
            return self._client
        async with self._lifecycle_lock:
            if self._client is None:
                self._client = await _docker_call(
                    docker.from_env, timeout=DOCKER_OP_TIMEOUT_S,
                    timeout_s=DOCKER_OP_TIMEOUT_S, op="from_env",
                )
        return self._client

    async def _get_build_client(self) -> Any:
        """Lazily build the long-timeout client used only for image build."""
        if self._build_client is not None:
            return self._build_client
        async with self._lifecycle_lock:
            if self._build_client is None:
                self._build_client = await _docker_call(
                    docker.from_env, timeout=float(_DEFAULT_BUILD_TIMEOUT_S),
                    timeout_s=float(_DEFAULT_BUILD_TIMEOUT_S), op="from_env_build",
                )
        return self._build_client

    async def _ensure_image(self) -> None:
        """Build the sandbox image once per process — never per lease.

        Guarded by its own lock (distinct from the pool's), so a first-run
        build cannot serialize behind — or be serialized by — unrelated lease
        acquisitions once the image is ready.
        """
        if self._image_ready:
            return
        async with self._image_lock:
            if self._image_ready:
                return
            client = await self._get_client()
            exists = await _docker_call(
                self._image_exists, client, timeout_s=DOCKER_OP_TIMEOUT_S, op="images.get",
            )
            if not exists:
                logger.info("Building %s — first-run cost, ~30-60s", _SANDBOX_IMAGE_TAG)
                build_client = await self._get_build_client()
                await _docker_call(
                    self._build_image_sync, build_client,
                    timeout_s=float(_DEFAULT_BUILD_TIMEOUT_S), op="images.build",
                )
            self._image_ready = True

    async def _create_lease_container(self, key: Tuple[str, str], mount_root: str) -> Any:
        """Build (or recreate) the named container for a fresh pool lease."""
        await self._ensure_image()
        client = await self._get_client()
        name = _lease_container_name(key)
        labels = {"ailienant.sandbox": "1", "ailienant.owner_port": _owner_port()}

        existing = await _docker_call(
            self._get_named_container_sync, client, name,
            timeout_s=DOCKER_OP_TIMEOUT_S, op="containers.get",
        )
        if existing is not None:
            # A same-named container surviving from a prior crash (missed by the
            # startup sweep, e.g. a manual restart) would otherwise 409-conflict
            # with `containers.run(name=...)` below.
            try:
                await _docker_call(
                    existing.remove, force=True, timeout_s=DOCKER_OP_TIMEOUT_S, op="remove_stale",
                )
            except docker.errors.NotFound:
                pass

        container = await _docker_call(
            self._run_container_sync, client, name, mount_root, labels,
            timeout_s=DOCKER_OP_TIMEOUT_S, op="containers.run",
        )
        self._emit_lifecycle("started", container)
        return container

    async def _revalidate(self, container: Any) -> bool:
        """True if a pooled lease's container object still exists and is running.

        A user ``docker rm``, a daemon restart, or a stale sweep can remove a
        container out from under a live lease; this makes that self-healing
        (transparent recreation on the next acquire) rather than a surfaced
        error. A daemon-unresponsive fault propagates as-is — the caller's own
        breaker-guarded creation attempt will fail identically and surface the
        adapter's degrade sentinel.
        """
        try:
            await _docker_call(container.reload, timeout_s=DOCKER_OP_TIMEOUT_S, op="reload")
        except docker.errors.NotFound:
            return False
        return str(getattr(container, "status", "")) == "running"

    async def _teardown_container(self, container: Any, *, reason: str) -> None:
        """Stop + remove one pooled container. Never raises — reap is best-effort."""
        try:
            await _docker_call(container.stop, timeout=10, timeout_s=DOCKER_OP_TIMEOUT_S, op="stop")
        except (SandboxDaemonTimeout, docker.errors.NotFound) as exc:
            logger.warning("Sandbox container stop failed (%s): %s", reason, exc)
        except Exception as exc:  # noqa: BLE001 — defensive cleanup, must never crash the pool
            logger.warning("Sandbox container stop failed (%s): %s", reason, exc, exc_info=True)
        try:
            await _docker_call(
                container.remove, force=True, timeout_s=DOCKER_OP_TIMEOUT_S, op="remove",
            )
        except docker.errors.NotFound:
            pass  # already gone — a concurrent removal race, not a fault
        except SandboxDaemonTimeout as exc:
            logger.warning("Sandbox container remove failed (%s): %s", reason, exc)
        except Exception as exc:  # noqa: BLE001 — defensive cleanup, must never crash the pool
            logger.warning("Sandbox container remove failed (%s): %s", reason, exc, exc_info=True)
        self._emit_lifecycle(reason, container)

    def _resolve_mount_root(self, session_id: Optional[str]) -> str:
        """The mount root a session's lease should bind — resolver, else shared default."""
        if session_id:
            resolver = _session_workspace_resolver
            if resolver is not None:
                try:
                    root = resolver(session_id)
                except Exception:  # noqa: BLE001 — a bad resolver must never break execution
                    logger.warning(
                        "session workspace resolver failed for %s", session_id, exc_info=True,
                    )
                    root = ""
                if root:
                    return root
        return self._host_workspace

    def _emit_lifecycle(self, event: str, container: Any = None) -> None:
        """Best-effort container-lifecycle telemetry, emitted on the event loop.

        Called only from the async lifecycle methods (never the thread-pooled
        sync helpers); the telemetry connection is check_same_thread=False and
        lock-serialized regardless. Never raises, so it can never affect the cage.
        """
        try:
            from core.telemetry import log_container_event
            cid = str(getattr(container, "id", "") or "")[:12]
            log_container_event(event, cid, _SANDBOX_IMAGE_TAG, "DOCKER")
        except Exception:  # noqa: BLE001 — telemetry must never affect the sandbox
            logger.debug("container lifecycle emit skipped (%s)", event, exc_info=True)

    # ── sync helpers (always called via _docker_call, off the event loop) ───

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

    def _get_named_container_sync(self, client: Any, name: str) -> Optional[Any]:
        try:
            return client.containers.get(name)
        except docker.errors.NotFound:
            return None

    def _run_container_sync(
        self, client: Any, name: str, mount_root: str, labels: Dict[str, str],
    ) -> Any:
        return client.containers.run(
            _SANDBOX_IMAGE_TAG,
            command=["tail", "-f", "/dev/null"],
            name=name,
            detach=True,
            read_only=True,
            network_mode="none",
            labels=labels,
            mem_limit=SANDBOX_MEM_LIMIT,
            pids_limit=SANDBOX_PIDS_LIMIT,
            volumes={
                mount_root: {
                    "bind": _CONTAINER_WORKDIR,
                    "mode": "ro",
                },
            },
            tmpfs={_CONTAINER_TMPFS_PATH: "rw,size=512m,nosuid,nodev"},
            working_dir=_CONTAINER_WORKDIR,
        )

    # ── pure helpers (no I/O) ───────────────────────────────────────────────

    def _translate_cwd(self, host_cwd: str, mount_root: str) -> str:
        """Map a host absolute path under ``mount_root`` into ``/workspace``.

        ``mount_root`` is the *lease's* mount root — not necessarily
        ``self._host_workspace`` — so a session's ``cwd`` is always translated
        against the project it was actually leased against. Falls back to the
        container workdir if the path escapes the mount — defence in depth
        against a stale ``cwd`` from a different workspace.
        """
        if not host_cwd:
            return _CONTAINER_WORKDIR
        host_abs = os.path.abspath(host_cwd)
        root_abs = os.path.abspath(mount_root)
        if host_abs == root_abs:
            return _CONTAINER_WORKDIR
        if host_abs.startswith(root_abs + os.sep):
            relative = host_abs[len(root_abs):].replace(os.sep, "/")
            return f"{_CONTAINER_WORKDIR}{relative}"
        logger.warning(
            "Sandbox cwd %r escapes lease mount root %r — falling back to %s",
            host_cwd, mount_root, _CONTAINER_WORKDIR,
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


async def sweep_orphaned_containers() -> None:
    """Startup reclamation: remove ``ailienant.sandbox`` containers this process
    does not own and whose recorded owner is no longer live.

    Concurrent backends are a real deployment shape (the extension spawns one
    per VS Code window on a dynamic port), so a blanket label sweep would
    force-remove a live sibling's containers. Liveness is decided the same way
    ``core.config.host_discovery.probe_host_alive`` already does for the
    external-gateway handshake — a TCP connect to the recorded loopback port —
    rather than a PID check, which that module documents as unreliable. A
    container is swept when its ``ailienant.owner_port`` label is absent (an
    old pre-12.6 singleton, or an unattributed manual start) or that port
    refuses a connection. Best-effort: a dead daemon or a mid-sweep removal
    race (``NotFound``) never raises past this function.
    """
    try:
        client = await _docker_call(
            docker.from_env, timeout=DOCKER_OP_TIMEOUT_S,
            timeout_s=DOCKER_OP_TIMEOUT_S, op="from_env_sweep",
        )
    except SandboxDaemonTimeout as exc:
        logger.warning("Sandbox startup sweep skipped — daemon unavailable: %s", exc)
        return

    try:
        containers = await _docker_call(
            _list_labeled_containers_sync, client,
            timeout_s=DOCKER_OP_TIMEOUT_S, op="containers.list",
        )
    except SandboxDaemonTimeout as exc:
        logger.warning("Sandbox startup sweep skipped — list failed: %s", exc)
        return

    from core.config.host_discovery import HostCoords, probe_host_alive

    my_port = _owner_port()
    for container in containers:
        labels = getattr(container, "labels", None) or {}
        owner_port = str(labels.get("ailienant.owner_port", "")).strip()
        name = str(getattr(container, "name", "") or "")
        is_legacy_singleton = name == _SANDBOX_CONTAINER_NAME
        if owner_port and owner_port == my_port:
            continue  # this process's own prior-run container
        alive = False
        if owner_port and not is_legacy_singleton:
            try:
                alive = await probe_host_alive(
                    HostCoords(port=int(owner_port), token=None, pid=0), timeout_sec=2.0,
                )
            except (ValueError, OSError):
                alive = False
        if alive:
            continue  # a live sibling backend owns this container — leave it
        try:
            await _docker_call(
                container.remove, force=True, timeout_s=DOCKER_OP_TIMEOUT_S, op="sweep_remove",
            )
            logger.info("Sandbox startup sweep removed orphaned container %s", name)
        except docker.errors.NotFound:
            pass
        except SandboxDaemonTimeout as exc:
            logger.warning("Sandbox startup sweep could not remove %s: %s", name, exc)


def _list_labeled_containers_sync(client: Any) -> List[Any]:
    return list(client.containers.list(all=True, filters={"label": "ailienant.sandbox=1"}))


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

    execution_source = "native_host"

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


# Process-wide host bridge, injected from the composition root (the FastAPI
# lifespan) via :func:`set_trusted_bridge`. Kept as a plain module global so
# ``core`` depends only on the ``HostExecutionBridge`` abstraction it owns and
# never imports the transport layer (dependency inversion): the concrete
# WebSocket bridge lives in ``api`` and is pushed down, not pulled up. ``None``
# until injected — the adapter degrades (and falls back) cleanly meanwhile.
_injected_trusted_bridge: Optional[HostExecutionBridge] = None


def set_trusted_bridge(bridge: Optional[HostExecutionBridge]) -> None:
    """Inject (or clear) the process-wide host bridge. Called once at startup.

    Passing ``None`` clears the seam — used by tests for isolation, mirroring the
    ``reset_task_service`` convention.
    """
    global _injected_trusted_bridge
    _injected_trusted_bridge = bridge


def _default_host_bridge() -> Optional[HostExecutionBridge]:
    """Resolve the process-wide host bridge injected at startup.

    Returns ``None`` until a concrete host bridge is injected via
    :func:`set_trusted_bridge`, which is the point at which trusted execution
    becomes routable. A ``None`` result makes the adapter degrade (and, when a
    fallback is configured, delegate to it) rather than crash, so the tier is
    safe to construct before the host channel exists.
    """
    return _injected_trusted_bridge


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

    execution_source = "devcontainer"
    supports_sessions = True

    def __init__(
        self,
        *,
        bridge: Optional[HostExecutionBridge] = None,
        host_workspace: Optional[str] = None,
        fallback: Optional[SandboxAdapter] = None,
    ) -> None:
        self._bridge: Optional[HostExecutionBridge] = bridge
        self._host_workspace: str = host_workspace or os.getcwd()
        self._provision_lock: asyncio.Lock = asyncio.Lock()
        self._provisioned: bool = False
        # Selective fallback: when the devcontainer infrastructure is unavailable
        # *before a command runs*, delegate to this adapter instead of degrading —
        # continuity for the autonomous loop. Left unset, the adapter degrades to a
        # bracketed sentinel exactly as before (backward-compatible).
        self._fallback: Optional[SandboxAdapter] = fallback

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
            return await self._fallback_or_degrade(
                sentinel="[devcontainer_bridge_unavailable]",
                command=command, timeout_s=timeout_s, cwd=cwd,
                env_whitelist=env_whitelist, session_id=session_id,
            )

        try:
            provisioned = await self._ensure_provisioned(
                bridge=bridge, session_id=session_id, cwd=cwd,
            )
        except asyncio.TimeoutError:
            await self._enqueue_dlq_stub(command="devcontainer up", cwd=cwd)
            return await self._fallback_or_degrade(
                sentinel="[devcontainer_provision_timeout]",
                command=command, timeout_s=timeout_s, cwd=cwd,
                env_whitelist=env_whitelist, session_id=session_id,
            )
        if not provisioned:
            return await self._fallback_or_degrade(
                sentinel="[devcontainer_provision_failed]",
                command=command, timeout_s=timeout_s, cwd=cwd,
                env_whitelist=env_whitelist, session_id=session_id,
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

    async def _fallback_or_degrade(
        self,
        *,
        sentinel: str,
        command: str,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: str,
    ) -> SandboxResult:
        """Delegate to the configured fallback, or return the degrade sentinel.

        Reached only from the *pre-execution* failure paths (bridge unavailable,
        provisioning failed/timed out) where the command provably never ran — so
        delegating to the fallback cannot double-apply a side effect (idempotency).
        Mid-execution failures never reach here; they degrade in place.
        """
        if self._fallback is None:
            return SandboxResult(exit_code=-1, stdout="", stderr=sentinel)
        logger.info(
            "Devcontainer unavailable (%s) — delegating to fallback adapter %s.",
            sentinel, type(self._fallback).__name__,
        )
        return await self._fallback.execute(
            command,
            timeout_s=timeout_s,
            cwd=cwd,
            env_whitelist=env_whitelist,
            session_id=session_id,
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

    execution_source = "wasm"

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

    This is also the one Docker call in the module a socket-level timeout
    cannot bound: a hijacked exec socket is a deliberately blocking raw read
    with no HTTP timeout underneath it (Phase 12.6's declared residual leak —
    contained, not eliminated, by the dedicated ``ail-docker`` pool + breaker).
    """

    def __init__(
        self,
        client: Any,
        container_id: str,
        cwd: str,
        env: Dict[str, str],
        argv: List[str],
        *,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_close = on_close
        self._close_fired = False
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
        # itself is released back to the pool (see `close`) and eventually
        # reaped by DockerSandboxAdapter.shutdown / idle-TTL eviction.
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        finally:
            self._fire_on_close()

    def _fire_on_close(self) -> None:
        """Release this session's container lease exactly once.

        `close()` is reachable twice in the normal teardown sequence —
        `terminate_tree()` calls it directly, and `_PtySession._teardown` calls
        it again afterward unconditionally — so without this guard the pool's
        refcount would be decremented twice for one session.
        """
        if self._close_fired:
            return
        self._close_fired = True
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:  # noqa: BLE001 — a lease-release fault must never break teardown
                logger.warning("sandbox lease release callback failed", exc_info=True)

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

    def get_sync_surface(self, cwd: str, session_id: Optional[str] = None) -> "SyncSurface":
        """Return a LocalFsSyncSurface rooted at the session's cwd."""
        del session_id  # host-native tier has no per-session container to key
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


# ── Trusted-tier selection (devcontainer + HITL-native fallback) ─────────────

_trusted_adapter: Optional["DevcontainerSandboxAdapter"] = None
_trusted_adapter_silent: Optional["DevcontainerSandboxAdapter"] = None


def get_trusted_adapter() -> SandboxAdapter:
    """Lazily-built singleton for *trusted* project execution.

    Routes to the user-owned devcontainer over the injected host bridge, with a
    :class:`NativeHITLSandboxAdapter` fallback so an unavailable devcontainer
    degrades to consent-gated host execution rather than halting the loop.

    Built with ``bridge=None`` so the adapter's ``_get_bridge()`` resolves
    :func:`_default_host_bridge` (the injected bridge) *per call* — the singleton
    therefore picks up the bridge regardless of build/injection order.
    """
    global _trusted_adapter
    if _trusted_adapter is None:
        _trusted_adapter = DevcontainerSandboxAdapter(
            bridge=None, fallback=NativeHITLSandboxAdapter(),
        )
    return _trusted_adapter


class _OracleFallbackAdapter(SandboxAdapter):
    """Per-call indirection to the resolved oracle tier (:func:`get_active_adapter`).

    ``DevcontainerSandboxAdapter``'s ``fallback`` is a concrete instance captured
    once at construction. ``get_active_adapter()``'s target, however, is
    reassigned during lifespan startup — capturing it eagerly at
    :func:`get_trusted_adapter_silent`'s first call risks freezing in a ``None``
    seen before startup finished. This shim re-resolves on every call instead,
    mirroring how :meth:`DevcontainerSandboxAdapter._get_bridge` already
    re-resolves its own dependency per call rather than capturing it once.
    """

    execution_source = "oracle_fallback"

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        adapter = get_active_adapter()
        if adapter is None:
            return SandboxResult(
                exit_code=-1, stdout="", stderr="[oracle_adapter_unresolved]",
            )
        return await adapter.execute(
            command,
            timeout_s=timeout_s,
            cwd=cwd,
            env_whitelist=env_whitelist,
            session_id=session_id,
        )


def get_trusted_adapter_silent() -> SandboxAdapter:
    """Lazily-built singleton for *trusted* execution with a NON-interactive fallback.

    Identical to :func:`get_trusted_adapter` except an unavailable devcontainer
    falls back to the locked oracle tier (:class:`_OracleFallbackAdapter`)
    instead of :class:`NativeHITLSandboxAdapter`. For non-interactive validation
    helpers (``check_type_integrity``, the coder's internal ``_exec``) that must
    never raise an approval card of their own — a caller that already gated
    consent (e.g. ``_gated_exec``'s HITL round-trip) routes through this so the
    devcontainer upgrade can never re-prompt on top of that consent.
    """
    global _trusted_adapter_silent
    if _trusted_adapter_silent is None:
        _trusted_adapter_silent = DevcontainerSandboxAdapter(
            bridge=None, fallback=_OracleFallbackAdapter(),
        )
    return _trusted_adapter_silent


def reset_trusted_adapter() -> None:
    """Drop the cached trusted adapters (test isolation)."""
    global _trusted_adapter, _trusted_adapter_silent
    _trusted_adapter = None
    _trusted_adapter_silent = None


def reset_sandbox_pool_state() -> None:
    """Drop module-level pool/breaker/DI state a unit test must not leak across.

    Resets the daemon breaker, the exec-client LRU cache, and the session
    workspace resolver seam. Does not touch ``ACTIVE_ADAPTER`` itself (a fresh
    :class:`DockerSandboxAdapter` instance owns its own ``_ContainerPool`` with
    no cross-test state to begin with).
    """
    reset_daemon_breaker()
    set_session_workspace_resolver(None)
    with _exec_client_cache_lock:
        _exec_client_cache.clear()


def resolve_execution_adapter(
    *,
    session_id: Optional[str],
    trusted: bool = True,
    interactive_fallback: bool = True,
) -> Optional[SandboxAdapter]:
    """Pick the adapter for a command.

    Trusted execution with a live session routes to the devcontainer tier; a
    provisioning failure then falls back to consent-gated host execution
    (:func:`get_trusted_adapter`) by default. Passing ``interactive_fallback=False``
    swaps that fallback for the locked oracle tier instead
    (:func:`get_trusted_adapter_silent`) — for non-interactive validation helpers
    that must never raise their own approval card. Everything else (no session,
    untrusted) keeps the locked oracle tier directly. The untrusted benchmark
    oracle never passes ``trusted=True`` here, so its Docker cage is provably
    untouched either way.
    """
    if trusted and session_id:
        return get_trusted_adapter() if interactive_fallback else get_trusted_adapter_silent()
    return get_active_adapter()


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
