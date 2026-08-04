# ailienant-core/tests/test_sandbox_pool_resilience.py
"""Phase 12.6 — Sandbox Reliability Hardening checkpoint suite.

Closes DEBT-097 (single shared Docker container across all concurrent
sessions) and DEBT-100 (a hung Docker daemon parks the worker thread
indefinitely). Hermetic: every Docker SDK entry point is a hand-rolled fake —
no live daemon, no ``docker`` network I/O — following the same-spirit
``MagicMock`` pattern already used by ``test_phase6_checkpoint_gate.py`` and
``test_container_lifecycle_telemetry.py``.

Covers, per the Phase 12.6 plan:
  POOL1-9  — per-(mount root, session) leasing, LRU eviction, same-mount
             sharing under exhaustion, cross-mount refusal, self-healing
             re-validation, guaranteed refcount release.
  PTY1-5   — an interactive session's lease is released exactly once on close
             (PTY1); the DEBT-150 hijacked-exec-socket bounds: an idle-but-live
             socket's reader thread still exits within the join timeout on
             close (PTY2), exec creation dispatches through the bounded
             ail-docker pool rather than the shared default executor (PTY3), a
             daemon fault mid-open releases the lease (PTY4), and recv_into's
             EOF (0) is distinguished from its timeout (PTY5).
  QUEUE1-6 — DEBT-151's FIFO admission queue: service order (QUEUE1), the
             depth-ceiling refusal is immediate (QUEUE2), a cancelled waiter
             hands off to its successor (QUEUE3), same-mount degrade still
             fires after a queued wait expires (QUEUE4), cross-mount refusal
             is unchanged (QUEUE5), and an already-held lease's re-acquire
             never queues behind pending waiters (QUEUE6).
  HANG1-7  — socket-timeout mapping, bounded-executor thread return, exec
             timeout-bucket safety, the daemon circuit breaker, and the
             dedicated ``ail-docker`` pool never touching the shared default.
  OOM1     — exit 137 is annotated with the memory-limit knob.
  RUNTIME1 — api/runtime.py's probes route through the same bounded dispatch.
  REAP1-3  — lifespan drain, sibling-safe startup sweep, NotFound tolerance.
  ADR1     — a daemon hang never re-runs resolve_default_adapter (ADR-001).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock

import docker.errors
import pytest
import requests

import core.sandbox as sandbox
from core.pty_session import _PtySession
from core.sandbox import (
    DockerSandboxAdapter,
    SandboxDaemonTimeout,
    SandboxResourceExhausted,
    SandboxSessionError,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_pool_tuning(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink wait/timeout knobs so the suite runs in well under a second."""
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 0.1)
    monkeypatch.setattr(sandbox, "DOCKER_OP_TIMEOUT_S", 2.0)
    sandbox.reset_sandbox_pool_state()
    yield
    sandbox.reset_sandbox_pool_state()


# ── fake Docker SDK primitives ────────────────────────────────────────────────


class _FakeSocket:
    """Stand-in for the raw socket ``exec_start(socket=True)`` hands back.

    Default mode: ``recv_into`` returns 0 (real EOF) immediately, so the PTY
    reader thread a session's ``start()`` spawns exits promptly instead of
    blocking on a real fd — most tests that open an interactive session need no
    further wiring. Construct with ``timeout_forever=True`` to instead make
    ``recv_into`` always raise ``TimeoutError`` — an idle-but-live connection
    that never yields data — exercising DEBT-150's deadline-poll loop in
    ``_DockerPtyBackend.read`` (PTY2/PTY5).
    """

    def __init__(self, *, timeout_forever: bool = False) -> None:
        self.timeout_forever = timeout_forever
        self.closed = False
        self.recv_into_calls = 0
        self.last_timeout: Optional[float] = None

    def setblocking(self, _flag: bool) -> None: ...

    def settimeout(self, value: Optional[float]) -> None:
        self.last_timeout = value

    def recv(self, _n: int) -> bytes:
        return b""

    def recv_into(self, buf: Any, nbytes: int = 0) -> int:
        self.recv_into_calls += 1
        if self.timeout_forever:
            raise TimeoutError("no data available")
        return 0

    def sendall(self, _b: bytes) -> None: ...

    def close(self) -> None:
        self.closed = True


class _FakeAPI:
    """Stand-in for ``docker.APIClient`` — only the exec trio is exercised."""

    def __init__(
        self,
        exit_code: int = 0,
        stdout: bytes = b"ok",
        stderr: bytes = b"",
        pty_socket_factory: Optional[Callable[[], _FakeSocket]] = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.exec_create_calls: List[Tuple[Any, ...]] = []
        self._pty_socket_factory = pty_socket_factory or _FakeSocket
        self.last_pty_socket: Optional[_FakeSocket] = None

    def exec_create(self, container_id: str, cmd: Any, **kwargs: Any) -> Dict[str, str]:
        self.exec_create_calls.append((container_id, cmd, kwargs))
        return {"Id": f"exec-{len(self.exec_create_calls)}"}

    def exec_start(
        self, exec_id: str, demux: bool = False, socket: bool = False, tty: bool = False,
    ) -> Any:
        if socket:
            sock = self._pty_socket_factory()
            self.last_pty_socket = sock
            return sock
        return (self.stdout, self.stderr)

    def exec_inspect(self, exec_id: str) -> Dict[str, int]:
        return {"ExitCode": self.exit_code}


class _FakeContainer:
    def __init__(self, name: str, mount_root: str, labels: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.id = f"cid-{name}"
        self.mount_root = mount_root
        self.labels = dict(labels or {})
        self.status = "running"
        self.stopped = False
        self.removed = False
        self.reload_calls = 0
        self.reload_raises: Optional[BaseException] = None

    def reload(self) -> None:
        self.reload_calls += 1
        if self.reload_raises is not None:
            raise self.reload_raises

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def remove(self, force: bool = True) -> None:
        if self.removed:
            raise docker.errors.NotFound("already removed")
        self.removed = True


class _FakeImages:
    def __init__(self, present: bool = True) -> None:
        self.present = present

    def get(self, tag: str) -> Any:
        if not self.present:
            raise docker.errors.ImageNotFound("no such image")
        return object()

    def build(self, **kwargs: Any) -> Tuple[Any, Any]:
        self.present = True
        return object(), iter(())


class _FakeContainers:
    def __init__(self, registry: Dict[str, _FakeContainer]) -> None:
        self._registry = registry
        self.run_calls: List[Dict[str, Any]] = []

    def get(self, name: str) -> _FakeContainer:
        c = self._registry.get(name)
        if c is None:
            raise docker.errors.NotFound(f"no such container: {name}")
        return c

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append(kwargs)
        name = kwargs["name"]
        mount_root = next(iter(kwargs["volumes"]))
        container = _FakeContainer(name, mount_root, labels=kwargs.get("labels"))
        self._registry[name] = container
        return container

    def list(self, all: bool = True, filters: Optional[Dict[str, Any]] = None) -> List[_FakeContainer]:
        return list(self._registry.values())


class _FakeClient:
    def __init__(self, registry: Optional[Dict[str, _FakeContainer]] = None, api: Optional[_FakeAPI] = None) -> None:
        self.registry: Dict[str, _FakeContainer] = registry if registry is not None else {}
        self.containers = _FakeContainers(self.registry)
        self.images = _FakeImages()
        self.api = api or _FakeAPI()
        self.closed = False
        self.ping_calls = 0

    def ping(self) -> bool:
        self.ping_calls += 1
        return True

    def info(self) -> Dict[str, str]:
        """``api/runtime.py``'s daemon probe uses ``info()``, not ``ping()``."""
        return {"ServerVersion": "27.0"}

    def close(self) -> None:
        self.closed = True


def _wire_fake_docker(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> List[Dict[str, Any]]:
    """Patch ``sandbox.docker.from_env`` to hand out ``client``; records kwargs."""
    calls: List[Dict[str, Any]] = []

    def _from_env(**kwargs: Any) -> _FakeClient:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(sandbox.docker, "from_env", _from_env)
    return calls


def _adapter(client: _FakeClient, monkeypatch: pytest.MonkeyPatch, host_workspace: str = "/default") -> DockerSandboxAdapter:
    _wire_fake_docker(monkeypatch, client)
    return DockerSandboxAdapter(host_workspace=host_workspace)


# ═══════════════════════════════════════════════════════════════════════════
# POOL — per-session leasing
# ═══════════════════════════════════════════════════════════════════════════


def test_POOL1_distinct_sessions_get_distinct_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch, host_workspace="/proj")

    async def _run() -> None:
        r1 = await adapter.execute("echo a", timeout_s=5, cwd="/proj", env_whitelist={}, session_id="s1")
        r2 = await adapter.execute("echo b", timeout_s=5, cwd="/proj", env_whitelist={}, session_id="s2")
        r3 = await adapter.execute("echo c", timeout_s=5, cwd="/proj", env_whitelist={}, session_id="s1")
        assert r1.exit_code == 0 and r2.exit_code == 0 and r3.exit_code == 0

    asyncio.run(_run())
    assert len(client.registry) == 2  # s1 and s2 got distinct containers; s1 reused


def test_POOL2_same_session_different_roots_get_distinct_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-12.6 defect: a session against a second project must NOT reuse
    the first project's container (and its mount) — each resolved root gets
    its own lease even for the identical session_id."""
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)
    roots = {"current": "/rootA"}
    sandbox.set_session_workspace_resolver(lambda sid: roots["current"])

    async def _run() -> None:
        await adapter.execute("echo a", timeout_s=5, cwd="/rootA", env_whitelist={}, session_id="s1")
        roots["current"] = "/rootB"
        await adapter.execute("echo b", timeout_s=5, cwd="/rootB", env_whitelist={}, session_id="s1")

    asyncio.run(_run())

    mounts = {c.mount_root for c in client.registry.values()}
    assert mounts == {os.path.abspath("/rootA"), os.path.abspath("/rootB")}
    assert len(client.registry) == 2


def test_POOL3_no_session_uses_shared_and_host_workspace_readable_first(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch, host_workspace="/oracle-root")
    # Readable before any lease exists (the oracle's materialize-before-execute contract).
    assert adapter.host_workspace == "/oracle-root"

    async def _run() -> None:
        await adapter.execute("echo a", timeout_s=5, cwd="/oracle-root", env_whitelist={})

    asyncio.run(_run())
    assert len(client.registry) == 1
    (container,) = client.registry.values()
    assert container.mount_root == os.path.abspath("/oracle-root")
    assert adapter.host_workspace == "/oracle-root"  # unchanged after a lease exists


def test_POOL4_cap_reached_with_idle_lease_evicts_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 2)
    adapter = _adapter(client, monkeypatch)

    async def _run() -> None:
        await adapter.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")
        await adapter.execute("echo b", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s2")
        # Both released (execute()'s finally); s1 is now the LRU idle lease.
        await adapter.execute("echo c", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s3")

    asyncio.run(_run())
    # s1's container was evicted (stopped+removed); a fresh one exists for s3.
    evicted = [c for c in client.registry.values() if c.stopped and c.removed]
    assert len(evicted) == 1
    live_names = {name for name, c in client.registry.items() if not c.removed}
    assert len(live_names) == 2


def test_POOL5_exhaustion_same_mount_shares_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    adapter = _adapter(client, monkeypatch)
    # Explicit: both sessions resolve to the SAME mount root ("/p") — the
    # scenario where sharing is safe.
    sandbox.set_session_workspace_resolver(lambda sid: "/p")

    async def _run() -> None:
        session1 = await adapter.open_session(cwd="/p", env_whitelist={}, session_id="s1")
        try:
            # s1's lease is held open (refcount=1, none idle) — s2 wants the same
            # mount root and must share rather than time out into an error.
            result = await adapter.execute("echo b", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s2")
            assert result.exit_code == 0
        finally:
            await session1.close()

    asyncio.run(_run())
    assert len(client.registry) == 1  # shared, not a second container


def test_POOL6_exhaustion_cross_mount_refuses_and_leaves_foreign_container(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    adapter = _adapter(client, monkeypatch)
    # Explicit: the two sessions resolve to DIFFERENT mount roots — sharing
    # would silently execute s2's command against s1's project.
    sandbox.set_session_workspace_resolver(
        lambda sid: {"s1": "/projA", "s2": "/projB"}.get(sid, "")
    )

    async def _run() -> Any:
        session1 = await adapter.open_session(cwd="/projA", env_whitelist={}, session_id="s1")
        try:
            return await adapter.execute(
                "echo b", timeout_s=5, cwd="/projB", env_whitelist={}, session_id="s2",
            )
        finally:
            await session1.close()

    result = asyncio.run(_run())
    assert result.exit_code == -1
    assert result.stderr == "[sandbox_pool_exhausted]"
    (only_container,) = client.registry.values()
    assert only_container.mount_root == os.path.abspath("/projA")
    assert not only_container.removed  # s1's container was never touched


def test_POOL7_refcount_released_even_when_exec_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    adapter = _adapter(client, monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("exec blew up")

    async def _run() -> None:
        # Scoped so `_run_exec_sync` is restored the moment the `with` exits —
        # unlike `monkeypatch.undo()`, this never touches the outer fixture's
        # own patches (SANDBOX_MAX_CONTAINERS, the fake docker.from_env wiring).
        with pytest.MonkeyPatch.context() as m:
            m.setattr(sandbox, "_run_exec_sync", _boom)
            with pytest.raises(RuntimeError):
                await adapter.execute(
                    "echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1",
                )

        # The lease was released despite the raise — a second call for the same
        # session must reuse the SAME container, not find it "stuck" forever.
        result = await adapter.execute(
            "echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1",
        )
        assert result.exit_code == 0

    asyncio.run(_run())
    assert len(client.registry) == 1


def test_POOL8_translate_cwd_uses_lease_mount_root(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch, host_workspace="/should-not-be-used")

    async def _run() -> None:
        sandbox.set_session_workspace_resolver(lambda sid: "/real-project")
        await adapter.execute(
            "pwd", timeout_s=5, cwd="/real-project/sub", env_whitelist={}, session_id="s1",
        )

    asyncio.run(_run())
    (_container_id, _cmd, kwargs) = client.api.exec_create_calls[0]
    assert kwargs["workdir"] == "/workspace/sub"


def test_POOL9_vanished_container_is_transparently_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    async def _run() -> None:
        await adapter.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")
        (container,) = client.registry.values()
        container.reload_raises = docker.errors.NotFound("gone")
        result = await adapter.execute("echo b", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")
        assert result.exit_code == 0

    asyncio.run(_run())
    # The vanished lease was dropped and a fresh container created under the same name.
    assert len(client.registry) == 1


# ═══════════════════════════════════════════════════════════════════════════
# PTY — interactive session lease release
# ═══════════════════════════════════════════════════════════════════════════


def test_PTY1_close_releases_lease_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: "/p")

    async def _run() -> None:
        session = await adapter.open_session(cwd="/p", env_whitelist={}, session_id="s1")
        lease = adapter._pool.peek("/p", "s1")
        assert lease is not None and lease.refcount == 1

        assert isinstance(session, _PtySession)
        backend = session._backend
        assert backend is not None
        backend.close()
        backend.close()  # idempotent — must not double-release

        # The release is scheduled via loop.call_soon_threadsafe, so poll a
        # few turns rather than assume it lands within a fixed sleep count.
        for _ in range(50):
            if lease.refcount == 0:
                break
            await asyncio.sleep(0)
        assert lease.refcount == 0

        # A second session for the SAME key can now acquire this now-idle lease.
        lease2 = await adapter._pool.acquire(mount_root="/p", session_id="s1")
        assert lease2 is lease
        assert lease2.refcount == 1
        await adapter._pool.release(lease2)

    asyncio.run(_run())


def test_PTY2_reader_thread_exits_on_close_despite_perpetual_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBT-150: a socket that only ever raises ``TimeoutError`` (an idle-but-live
    connection) must not leak the pty-reader thread. ``close()`` flips
    ``_DockerPtyBackend._closed`` before tearing down the socket, so the reader's
    in-flight/poll-looped ``recv_into`` sees closure and returns EOF well inside
    ``pty_session._JOIN_TIMEOUT_S`` — no "did not terminate" leak."""
    monkeypatch.setattr(sandbox, "_PTY_SOCK_POLL_S", 0.01)  # fast poll for a fast test
    api = _FakeAPI(pty_socket_factory=lambda: _FakeSocket(timeout_forever=True))
    client = _FakeClient(api=api)
    adapter = _adapter(client, monkeypatch)

    async def _run() -> None:
        session = await adapter.open_session(cwd="/p", env_whitelist={}, session_id="pty2")
        assert isinstance(session, _PtySession)
        assert api.last_pty_socket is not None
        assert api.last_pty_socket.recv_into_calls > 0, "reader thread must be polling"

        # close() must return promptly — the reader thread's join is bounded by
        # pty_session._JOIN_TIMEOUT_S (2.0s); this asserts well under that.
        await asyncio.wait_for(session.close(), timeout=1.0)

        reader = session._reader
        assert reader is not None and not reader.is_alive(), "reader thread leaked"

    asyncio.run(_run())


def test_PTY3_exec_creation_dispatches_on_ail_docker_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBT-150: PTY exec creation must run on the bounded ``ail-docker`` pool,
    never the shared default executor ``_PtySession.start()``'s own
    ``asyncio.to_thread`` would otherwise use (mirrors HANG6's assertion for the
    module's other blocking Docker calls)."""
    seen_thread_names: List[str] = []
    real_exec_create = _FakeAPI.exec_create

    def _spying_exec_create(self: _FakeAPI, container_id: str, cmd: Any, **kwargs: Any) -> Dict[str, str]:
        seen_thread_names.append(threading.current_thread().name)
        return real_exec_create(self, container_id, cmd, **kwargs)

    monkeypatch.setattr(_FakeAPI, "exec_create", _spying_exec_create)
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    async def _run() -> None:
        session = await adapter.open_session(cwd="/p", env_whitelist={}, session_id="pty3")
        await session.close()

    asyncio.run(_run())
    assert seen_thread_names and seen_thread_names[0].startswith("ail-docker")


def test_PTY4_daemon_fault_during_open_releases_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBT-150: if exec creation raises (daemon fault) after the lease was
    already acquired, ``open_session`` must release it — otherwise the container
    is stranded at refcount>=1 forever, the same permanent-occupancy failure
    DEBT-152 closes on the run-lifecycle side."""
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise requests.exceptions.ReadTimeout("daemon did not respond")

    monkeypatch.setattr(sandbox, "_create_pty_exec", AsyncMock(side_effect=SandboxDaemonTimeout("x")))

    async def _run() -> None:
        with pytest.raises(SandboxSessionError):
            await adapter.open_session(cwd="/p", env_whitelist={}, session_id="pty4")

        # The lease must be idle (refcount 0) — not stranded — so a fresh
        # acquire for the same key reuses it rather than hitting the pool cap.
        # No session-workspace resolver is set here, so the lease was keyed by
        # the adapter's own host_workspace (the fallback _resolve_mount_root
        # takes), not the "/p" passed to open_session as cwd.
        lease = adapter._pool.peek(adapter.host_workspace, "pty4")
        for _ in range(50):
            if lease is not None and lease.refcount == 0:
                break
            await asyncio.sleep(0)
        assert lease is not None and lease.refcount == 0

    asyncio.run(_run())


def test_PTY5_recv_into_eof_distinct_from_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEBT-150: ``recv_into`` returning 0 (real EOF) must end the read immediately,
    while ``TimeoutError`` must NOT be mistaken for EOF — conflating the two is
    exactly the bug class this closes (a live-but-idle session killed early, or an
    actually-dead one polled forever)."""
    from core.sandbox import _DockerPtyBackend

    class _SequencedSocket(_FakeSocket):
        def __init__(self) -> None:
            super().__init__()
            self._script: List[Any] = [TimeoutError(), TimeoutError(), 0]

        def recv_into(self, buf: Any, nbytes: int = 0) -> int:
            self.recv_into_calls += 1
            outcome = self._script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    sock = _SequencedSocket()
    backend = _DockerPtyBackend(sock, api=_FakeAPI(), exec_id="exec-1")

    result = backend.read(4096)

    assert result == b"", "recv_into -> 0 must be treated as real EOF"
    assert sock.recv_into_calls == 3, "both TimeoutErrors must be polled through, not treated as EOF"


# ═══════════════════════════════════════════════════════════════════════════
# QUEUE — bounded FIFO admission queue (DEBT-151)
# ═══════════════════════════════════════════════════════════════════════════


def test_QUEUE1_fifo_service_order(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 5.0)
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: {"a": "/rootA", "b": "/rootB", "c": "/rootC"}.get(sid, ""))

    order: List[str] = []

    async def _run() -> None:
        lease_a = await adapter._pool.acquire(mount_root="/rootA", session_id="a")

        async def _wait_for(name: str, root: str) -> None:
            await adapter._pool.acquire(mount_root=root, session_id=name)
            order.append(name)

        # b then c queue, in that order, behind a's held lease.
        tb = asyncio.ensure_future(_wait_for("b", "/rootB"))
        await asyncio.sleep(0)  # let b enqueue first
        tc = asyncio.ensure_future(_wait_for("c", "/rootC"))
        await asyncio.sleep(0)  # let c enqueue second

        await adapter._pool.release(lease_a)  # frees capacity for exactly one waiter
        await asyncio.wait_for(tb, timeout=2.0)
        lease_b = adapter._pool.peek("/rootB", "b")
        assert lease_b is not None
        await adapter._pool.release(lease_b)
        await asyncio.wait_for(tc, timeout=2.0)
        lease_c = adapter._pool.peek("/rootC", "c")
        assert lease_c is not None
        await adapter._pool.release(lease_c)

    asyncio.run(_run())
    assert order == ["b", "c"], "waiters must be served in arrival order"


def test_QUEUE2_depth_ceiling_refuses_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_QUEUED", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 30.0)  # would stall the test if reached
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: {"a": "/rootA", "b": "/rootB", "c": "/rootC"}.get(sid, ""))

    async def _run() -> None:
        await adapter._pool.acquire(mount_root="/rootA", session_id="a")
        # One waiter fills the queue (depth ceiling = 1).
        blocked = asyncio.ensure_future(adapter._pool.acquire(mount_root="/rootB", session_id="b"))
        await asyncio.sleep(0)

        start = time.monotonic()
        with pytest.raises(SandboxResourceExhausted):
            await adapter._pool.acquire(mount_root="/rootC", session_id="c")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "depth-ceiling refusal must be immediate, not wait out the lease timeout"

        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked

    asyncio.run(_run())


def test_QUEUE3_cancelled_waiter_does_not_strand_its_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 5.0)
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: {"a": "/rootA", "b": "/rootB", "c": "/rootC"}.get(sid, ""))

    async def _run() -> None:
        lease_a = await adapter._pool.acquire(mount_root="/rootA", session_id="a")

        tb = asyncio.ensure_future(adapter._pool.acquire(mount_root="/rootB", session_id="b"))
        await asyncio.sleep(0)
        tc = asyncio.ensure_future(adapter._pool.acquire(mount_root="/rootC", session_id="c"))
        await asyncio.sleep(0)

        tb.cancel()  # b was ahead of c in the queue
        with pytest.raises(asyncio.CancelledError):
            await tb

        await adapter._pool.release(lease_a)
        lease_c = await asyncio.wait_for(tc, timeout=2.0)
        assert lease_c.mount_root == os.path.abspath("/rootC")
        await adapter._pool.release(lease_c)

    asyncio.run(_run())


def test_QUEUE4_share_degrade_still_fires_after_queued_wait_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 0.05)
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: "/p")

    async def _run() -> None:
        lease_a = await adapter._pool.acquire(mount_root="/p", session_id="a")
        try:
            # Same mount root as the held lease — must degrade to a share once
            # the queued wait times out, exactly as the pre-queue behavior did.
            lease_b = await adapter._pool.acquire(mount_root="/p", session_id="b")
            assert lease_b is lease_a
            assert lease_b.refcount == 2
            await adapter._pool.release(lease_b)
        finally:
            await adapter._pool.release(lease_a)

    asyncio.run(_run())


def test_QUEUE5_cross_mount_refusal_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 0.05)
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: {"a": "/projA", "b": "/projB"}.get(sid, ""))

    async def _run() -> None:
        lease_a = await adapter._pool.acquire(mount_root="/projA", session_id="a")
        try:
            with pytest.raises(SandboxResourceExhausted):
                await adapter._pool.acquire(mount_root="/projB", session_id="b")
        finally:
            await adapter._pool.release(lease_a)

    asyncio.run(_run())


def test_QUEUE6_existing_lease_reacquire_skips_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEBT-151: a session that already holds a lease must re-acquire it directly
    (refcount bump) rather than queueing behind unrelated new admissions — a
    second command on an in-flight session must never stall behind a burst of
    other sessions' first commands."""
    client = _FakeClient()
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_CONTAINERS", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_MAX_QUEUED", 1)
    monkeypatch.setattr(sandbox, "SANDBOX_LEASE_WAIT_S", 30.0)  # would stall the test if reached
    adapter = _adapter(client, monkeypatch)
    sandbox.set_session_workspace_resolver(lambda sid: {"a": "/rootA", "b": "/rootB"}.get(sid, ""))

    async def _run() -> None:
        lease_a = await adapter._pool.acquire(mount_root="/rootA", session_id="a")
        # Fill the queue to its ceiling with an unrelated waiter.
        blocked = asyncio.ensure_future(adapter._pool.acquire(mount_root="/rootB", session_id="b"))
        await asyncio.sleep(0)

        # "a" re-acquiring its OWN lease must succeed immediately — the queue
        # being full must not affect a caller that already holds a valid lease.
        lease_a2 = await asyncio.wait_for(
            adapter._pool.acquire(mount_root="/rootA", session_id="a"), timeout=1.0,
        )
        assert lease_a2 is lease_a
        assert lease_a2.refcount == 2

        await adapter._pool.release(lease_a2)
        await adapter._pool.release(lease_a)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked

    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════
# HANG — daemon-hang containment (DEBT-100)
# ═══════════════════════════════════════════════════════════════════════════


def test_HANG1_read_timeout_degrades_to_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise requests.exceptions.ReadTimeout("daemon did not respond")

    monkeypatch.setattr(sandbox, "_run_exec_sync", _boom)

    async def _run() -> Any:
        return await adapter.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")

    result = asyncio.run(_run())
    assert result.exit_code == -1
    assert result.stderr == "[sandbox_daemon_unavailable]"


def test_HANG2_worker_returns_to_pool_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine wait_for timeout (slow fn) still frees the calling coroutine
    promptly and the executor accepts further work afterward — O(1) release."""

    def _slow(*_a: Any, **_k: Any) -> str:
        time.sleep(0.3)
        return "done"

    async def _run() -> None:
        with pytest.raises(SandboxDaemonTimeout):
            await sandbox._docker_call(_slow, timeout_s=0.02, op="slow_probe")
        # The executor must still accept and complete a fresh, fast call.
        result = await sandbox._docker_call(lambda: "ok", timeout_s=1.0, op="fast_probe")
        assert result == "ok"

    asyncio.run(_run())


def test_HANG3_exec_client_bucketed_and_lru_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    calls = _wire_fake_docker(monkeypatch, client)

    async def _run() -> None:
        c1 = await sandbox._get_exec_client(45.0)  # → bucket 60
        c2 = await sandbox._get_exec_client(58.0)  # → bucket 60 (cache hit)
        assert c1 is c2
        for n in range(1, 12):  # overflow the 8-entry cache
            await sandbox._get_exec_client(n * 30.0 + 1.0)

    asyncio.run(_run())
    assert len(sandbox._exec_client_cache) <= sandbox._EXEC_CLIENT_CACHE_CAP
    # First bucket request used a timeout >= the caller's own budget.
    assert calls[0]["timeout"] >= 45.0


def test_HANG4_breaker_opens_after_two_faults_and_stops_dispatching(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def _boom() -> None:
        attempts["n"] += 1
        raise requests.exceptions.ConnectionError("no daemon")

    async def _run() -> None:
        for _ in range(2):
            with pytest.raises(SandboxDaemonTimeout):
                await sandbox._docker_call(_boom, timeout_s=1.0, op="p")
        assert sandbox.is_daemon_breaker_open() is True
        with pytest.raises(SandboxDaemonTimeout):
            await sandbox._docker_call(_boom, timeout_s=1.0, op="p")
        # The breaker refused the third call outright — fn was never invoked again.
        assert attempts["n"] == 2

    asyncio.run(_run())


def test_HANG5_open_session_raises_session_error_when_breaker_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    for _ in range(2):
        sandbox._daemon_breaker.record_failure()
    assert sandbox.is_daemon_breaker_open() is True

    async def _run() -> None:
        with pytest.raises(SandboxSessionError):
            await adapter.open_session(cwd="/p", env_whitelist={}, session_id="s1")

    asyncio.run(_run())


def test_HANG6_docker_calls_use_dedicated_thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_thread_names: List[str] = []

    def _record() -> str:
        seen_thread_names.append(threading.current_thread().name)
        return "ok"

    async def _run() -> None:
        await sandbox._docker_call(_record, timeout_s=1.0, op="p")

    asyncio.run(_run())
    assert seen_thread_names and seen_thread_names[0].startswith("ail-docker")


def test_HANG7_breaker_closes_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = sandbox._DaemonBreaker(fail_threshold=2, cooldown_s=0.05)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True
    time.sleep(0.06)
    assert breaker.is_open is False  # cooldown elapsed — half-open probe allowed
    breaker.record_success()
    assert breaker.is_open is False


# ═══════════════════════════════════════════════════════════════════════════
# OOM — exit 137 annotation
# ═══════════════════════════════════════════════════════════════════════════


def test_OOM1_exit_137_names_the_memory_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(api=_FakeAPI(exit_code=137, stdout=b"", stderr=b""))
    adapter = _adapter(client, monkeypatch)

    async def _run() -> Any:
        return await adapter.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")

    result = asyncio.run(_run())
    assert result.exit_code == 137
    assert "AILIENANT_SANDBOX_MEM_LIMIT" in result.stderr
    assert "[sandbox_oom]" in result.stderr

    # Exit 124 (GNU timeout) keeps its own, distinct note — not conflated with OOM.
    # The exec-client cache is keyed only by timeout bucket (by design — in
    # production there is exactly one daemon regardless of adapter instance),
    # so a fresh fake client for this second scenario must not be shadowed by
    # the first one already cached under the same bucket.
    sandbox.reset_sandbox_pool_state()
    client2 = _FakeClient(api=_FakeAPI(exit_code=124, stdout=b"", stderr=b""))
    adapter2 = _adapter(client2, monkeypatch)

    async def _run2() -> Any:
        return await adapter2.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")

    result2 = asyncio.run(_run2())
    assert "[sandbox_timeout]" in result2.stderr
    assert "AILIENANT_SANDBOX_MEM_LIMIT" not in result2.stderr


# ═══════════════════════════════════════════════════════════════════════════
# RUNTIME — api/runtime.py routes through the same bounded dispatch
# ═══════════════════════════════════════════════════════════════════════════


def test_RUNTIME1_probe_docker_uses_docker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.runtime as runtime_mod

    runtime_mod._docker_cache = {}
    used: Dict[str, bool] = {"docker_call": False}
    real_docker_call = sandbox.docker_call

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        used["docker_call"] = True
        return await real_docker_call(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "docker_call", _spy)
    client = _FakeClient()
    monkeypatch.setattr(runtime_mod.docker, "from_env", lambda **kw: client)

    result = asyncio.run(runtime_mod._probe_docker(force=True))
    assert result is True
    assert used["docker_call"] is True


# ═══════════════════════════════════════════════════════════════════════════
# REAP — lifespan drain, sibling-safe startup sweep
# ═══════════════════════════════════════════════════════════════════════════


def test_REAP1_drain_stops_and_removes_every_lease_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    async def _run() -> None:
        await adapter.execute("echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1")
        await adapter.execute("echo b", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s2")
        await adapter.shutdown()
        await adapter.shutdown()  # idempotent re-call

    asyncio.run(_run())
    assert len(client.registry) == 2
    assert all(c.stopped and c.removed for c in client.registry.values())


def test_REAP2_sweep_removes_dead_owner_keeps_live_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    registry: Dict[str, _FakeContainer] = {}
    dead = _FakeContainer("ailienant-sandbox-dead", "/p", labels={"ailienant.sandbox": "1", "ailienant.owner_port": "9001"})
    alive = _FakeContainer("ailienant-sandbox-alive", "/p", labels={"ailienant.sandbox": "1", "ailienant.owner_port": "9002"})
    registry["ailienant-sandbox-dead"] = dead
    registry["ailienant-sandbox-alive"] = alive
    client = _FakeClient(registry=registry)
    _wire_fake_docker(monkeypatch, client)
    monkeypatch.setenv("AILIENANT_API_PORT", "7000")  # neither container is "mine"

    async def _fake_probe_host_alive(coords: Any, timeout_sec: float = 2.0) -> bool:
        return coords.port == 9002

    monkeypatch.setattr("core.config.host_discovery.probe_host_alive", _fake_probe_host_alive)

    asyncio.run(sandbox.sweep_orphaned_containers())

    assert dead.removed is True
    assert alive.removed is False


def test_REAP3_notfound_mid_sweep_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    registry: Dict[str, _FakeContainer] = {}
    gone = _FakeContainer("ailienant-sandbox-gone", "/p", labels={"ailienant.sandbox": "1", "ailienant.owner_port": "9999"})
    gone.removed = True  # simulate: already removed by a concurrent race
    registry["ailienant-sandbox-gone"] = gone
    client = _FakeClient(registry=registry)
    _wire_fake_docker(monkeypatch, client)
    monkeypatch.setenv("AILIENANT_API_PORT", "7000")

    async def _fake_probe_host_alive(coords: Any, timeout_sec: float = 2.0) -> bool:
        return False

    monkeypatch.setattr("core.config.host_discovery.probe_host_alive", _fake_probe_host_alive)

    # `container.remove()` on an already-removed fake raises NotFound — must not propagate.
    asyncio.run(sandbox.sweep_orphaned_containers())


# ═══════════════════════════════════════════════════════════════════════════
# ADR — ADR-001 lock: a daemon hang never re-resolves the tier
# ═══════════════════════════════════════════════════════════════════════════


def test_ADR1_daemon_hang_does_not_reresolve_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    adapter = _adapter(client, monkeypatch)

    saved = (sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER)
    try:
        sandbox.ACTIVE_TIER = "DOCKER"
        sandbox.ACTIVE_ADAPTER = adapter

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise requests.exceptions.ReadTimeout("daemon hung")

        monkeypatch.setattr(sandbox, "_run_exec_sync", _boom)

        async def _run() -> Any:
            return await adapter.execute(
                "echo a", timeout_s=5, cwd="/p", env_whitelist={}, session_id="s1",
            )

        result = asyncio.run(_run())
        assert result.stderr == "[sandbox_daemon_unavailable]"
        assert sandbox.get_active_tier() == "DOCKER"
        assert sandbox.get_active_adapter() is adapter
    finally:
        sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER = saved
