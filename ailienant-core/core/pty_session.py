# ailienant-core/core/pty_session.py
"""Persistent, bidirectional, non-blocking pseudo-terminal sessions.

A :class:`SandboxSession` is an interactive shell that survives across
commands: one long-lived shell process owns ``cwd``/``env`` for the session
lifetime, so sequential commands observe each other's directory changes and
exported variables. Output is surfaced as a stream of raw byte deltas
(``AsyncIterator[bytes]``) rather than a single terminal buffer, and the caller
can feed ``stdin``, send an interrupt (Ctrl-C), or tear down the whole process
tree.

Design invariants:

* **Event loop is never blocked.** The only blocking primitive (a read on the
  PTY master / pipe) lives exclusively in a dedicated reader thread. The loop
  side only ever awaits an :class:`asyncio.Queue`, so a hung command parks the
  awaiting coroutine instead of freezing the loop.
* **Backpressure is lossless.** The reader thread hands bytes to the loop via
  ``run_coroutine_threadsafe(queue.put(...)).result()``; a full bounded queue
  blocks the reader thread, propagating OS-buffer backpressure to the child.
  Bytes are never dropped (dropping would split a UTF-8 sequence and break the
  consumer's incremental decoder).
* **No orphaned threads or descriptors.** Teardown closes the master
  descriptor/handle, which unblocks the reader thread's read, then joins it.
* **Terminal echo is disabled.** A PTY echoes ``stdin`` to ``stdout`` by
  default; left on, the shell would echo the end-of-command sentinel and the
  parser would declare the command finished prematurely. Unix clears the
  ``ECHO`` termios flag; the sentinel itself is also chosen so an echoed
  command line can never match the boundary regex.

The session never decodes output — bytes are the contract precisely so a
multibyte character is never split into mojibake mid-stream; decoding is the
consumer's responsibility (e.g. an incremental UTF-8 decoder).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger("AILIENANT_PTY")

# ── Tunables ─────────────────────────────────────────────────────────────────

_READ_CHUNK: int = 4096
_QUEUE_MAXSIZE: int = 256
_CLOSE_POLL_S: float = 0.1
_JOIN_TIMEOUT_S: float = 2.0
_GRACE_KILL_S: float = 0.5

# A pre-spawn guard returns a non-None reason string to veto a command.
PreSpawnGuard = Callable[[str], Optional[str]]


class SandboxSessionError(RuntimeError):
    """Raised when a session command is vetoed or the session is misused."""


# ── Abstract session contract ────────────────────────────────────────────────


class SandboxSession(ABC):
    """A persistent interactive shell inside an adapter's isolation envelope.

    One long-lived shell owns ``cwd``/``env`` for the session lifetime; all I/O
    is non-blocking with respect to the event loop.
    """

    @abstractmethod
    async def start(self) -> None:
        """Spawn the persistent shell and begin streaming. Idempotent."""

    @abstractmethod
    async def run(self, command: str, *, timeout_s: float) -> int:
        """Write ``command`` to the shell; return its exit code once observed.

        Output is surfaced via :meth:`stream`, not returned. Raises
        :class:`asyncio.TimeoutError` if the command does not complete within
        ``timeout_s`` (the session stays usable; the caller may interrupt/kill).
        """

    @abstractmethod
    def stream(self) -> AsyncIterator[bytes]:
        """Async iterator of raw output deltas. Yields as data arrives."""

    @abstractmethod
    async def write_stdin(self, data: bytes) -> None:
        """Feed bytes to the running foreground process's stdin."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Send a Ctrl-C-equivalent to the foreground group. Shell survives."""

    @abstractmethod
    async def kill(self) -> None:
        """Terminate the whole process tree and reap. No zombies."""

    @abstractmethod
    async def close(self) -> None:
        """Graceful teardown; falls back to :meth:`kill`. Idempotent."""

    async def __aenter__(self) -> "SandboxSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ── PTY backend strategy ─────────────────────────────────────────────────────


class _PtyBackend(ABC):
    """Synchronous, blocking transport beneath a :class:`_PtySession`.

    All methods are blocking and are driven from the session's reader thread
    (reads) or via :func:`asyncio.to_thread` (writes/teardown). Concrete
    backends never touch the event loop.
    """

    @property
    @abstractmethod
    def pid(self) -> Optional[int]:
        ...

    @abstractmethod
    def read(self, size: int) -> bytes:
        """Blocking read; returns ``b""`` on EOF, raises ``OSError`` on close."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        ...

    @abstractmethod
    def send_interrupt(self) -> None:
        ...

    @abstractmethod
    def terminate_tree(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the master descriptor/handle, unblocking a pending read."""

    @abstractmethod
    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        ...


BackendFactory = Callable[[List[str], str, Dict[str, str], bytes], _PtyBackend]


# ── Persistent session ───────────────────────────────────────────────────────


class _PtySession(SandboxSession):
    """Concrete session over a :class:`_PtyBackend`.

    A reader thread drains the backend into ``_raw_q``; a demux coroutine scans
    ``_raw_q`` for the per-command sentinel, splits control from output, pushes
    clean output to ``_out_q`` (the public :meth:`stream`), and resolves each
    :meth:`run` future with the parsed exit code.
    """

    def __init__(
        self,
        *,
        cwd: str,
        env: Dict[str, str],
        shell_argv: Optional[List[str]] = None,
        shell_kind: Optional[str] = None,
        pre_spawn_guard: Optional[PreSpawnGuard] = None,
        backend_factory: Optional[BackendFactory] = None,
    ) -> None:
        self._cwd = cwd
        self._env = dict(env)
        self._shell_kind = shell_kind or ("cmd" if sys.platform == "win32" else "posix")
        self._shell_argv = shell_argv or self._default_shell_argv()
        self._pre_spawn_guard = pre_spawn_guard
        self._backend_factory = backend_factory or _default_backend_factory

        # A unique, control-char-prefixed marker. Control bytes 0x01/0x02 do not
        # appear in normal program output, and the resolved boundary line
        # (marker + digits) never collides with the echoed command that emits
        # it (which carries the format spec, not the resolved code).
        self._marker = b"\x01\x02" + uuid4().hex.encode("ascii")
        self._boundary = re.compile(
            re.escape(self._marker) + rb"(\d+)\r?\n"
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._backend: Optional[_PtyBackend] = None
        self._raw_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._reader: Optional[threading.Thread] = None
        self._demux_task: Optional["asyncio.Task[None]"] = None
        self._pending: Optional["asyncio.Future[int]"] = None
        self._closing = False
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _default_shell_argv(self) -> List[str]:
        if self._shell_kind == "cmd":
            return ["cmd.exe"]
        return ["/bin/sh"]

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._raw_q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._out_q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._backend = await asyncio.to_thread(
            self._backend_factory, self._shell_argv, self._cwd, self._env, self._marker,
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="pty-reader", daemon=True,
        )
        self._reader.start()
        self._demux_task = self._loop.create_task(self._demux_loop())
        self._started = True

    # ── reader thread (off the event loop) ───────────────────────────────────

    def _read_loop(self) -> None:
        backend = self._backend
        assert backend is not None
        try:
            while not self._closing:
                data = backend.read(_READ_CHUNK)
                if not data:
                    break
                self._offer(data)
        except OSError:
            # Expected when the master descriptor is closed during teardown.
            pass
        except Exception as exc:  # noqa: BLE001 — never let the thread die silently
            logger.warning("pty reader thread error: %s", exc)
        finally:
            self._offer(None)

    def _offer(self, item: Optional[bytes]) -> None:
        """Hand an item to the loop with lossless backpressure.

        Blocks the reader thread while the bounded queue is full so the OS
        buffer fills and the child pauses. Abandons only on teardown.
        """
        loop = self._loop
        if loop is None:
            return
        if self._closing and item is not None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._raw_q.put(item), loop)
        except RuntimeError:
            return  # loop already closed
        while True:
            try:
                fut.result(timeout=_CLOSE_POLL_S)
                return
            except concurrent.futures.TimeoutError:
                if self._closing:
                    fut.cancel()
                    return
            except Exception:  # noqa: BLE001 — loop closed mid-put
                return

    # ── demux coroutine (on the event loop) ──────────────────────────────────

    async def _demux_loop(self) -> None:
        buf = bytearray()
        while True:
            item = await self._raw_q.get()
            if item is None:
                await self._out_q.put(None)
                return
            buf.extend(item)
            emit, buf = self._drain_boundaries(buf)
            if emit:
                await self._out_q.put(bytes(emit))

    def _drain_boundaries(self, buf: bytearray) -> "tuple[bytearray, bytearray]":
        """Split completed-command sentinels out of ``buf``.

        Returns ``(bytes_to_emit, remaining_buf)``. Each sentinel found resolves
        the pending :meth:`run` future with its exit code and is stripped from
        the stream. A trailing partial sentinel is retained for the next chunk;
        everything else is emitted immediately so interactive prompts (which
        carry no newline) are never withheld.
        """
        emit = bytearray()
        while True:
            match = self._boundary.search(buf)
            if match is None:
                keep = _partial_suffix_len(buf, self._marker)
                cut = len(buf) - keep
                emit.extend(buf[:cut])
                return emit, buf[cut:]
            emit.extend(buf[: match.start()])
            code = int(match.group(1))
            self._resolve(code)
            buf = bytearray(buf[match.end():])

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
        loop = self._loop
        assert loop is not None
        self._pending = loop.create_future()
        await self._write(self._compose(command))
        return await asyncio.wait_for(self._pending, timeout=timeout_s)

    def stream(self) -> AsyncIterator[bytes]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._out_q.get()
            if item is None:
                return
            yield item

    async def write_stdin(self, data: bytes) -> None:
        await self._write(data)

    async def interrupt(self) -> None:
        backend = self._backend
        if backend is not None:
            await asyncio.to_thread(backend.send_interrupt)

    async def kill(self) -> None:
        await self._teardown(graceful=False)

    async def close(self) -> None:
        await self._teardown(graceful=True)

    # ── teardown ─────────────────────────────────────────────────────────────

    async def _teardown(self, *, graceful: bool) -> None:
        if not self._started:
            return
        self._closing = True
        backend = self._backend
        if backend is not None:
            try:
                await asyncio.to_thread(backend.terminate_tree)
            except Exception as exc:  # noqa: BLE001 — defensive cleanup
                logger.warning("pty terminate_tree failed: %s", exc)
            # Closing the descriptor unblocks the reader thread's pending read.
            try:
                await asyncio.to_thread(backend.close)
            except Exception as exc:  # noqa: BLE001 — defensive cleanup
                logger.warning("pty close failed: %s", exc)
        reader = self._reader
        if reader is not None:
            await asyncio.to_thread(reader.join, _JOIN_TIMEOUT_S)
            if reader.is_alive():
                logger.warning("pty reader thread did not terminate within join timeout")
        if backend is not None:
            try:
                await asyncio.to_thread(backend.wait, _JOIN_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — reap best-effort
                logger.warning("pty reap failed: %s", exc)
        # Let the demux drain the EOF that the reader emitted so any consumer of
        # stream() receives its close sentinel; cancel only if it stalls.
        task = self._demux_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_JOIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                task.cancel()
                self._force_out_eof()
            except asyncio.CancelledError:
                pass
        self._started = False

    def _force_out_eof(self) -> None:
        """Best-effort guarantee that a stalled :meth:`stream` sees its sentinel."""
        try:
            self._out_q.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._out_q.get_nowait()
                self._out_q.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _write(self, payload: bytes) -> None:
        backend = self._backend
        if backend is None:
            raise SandboxSessionError("session not started")
        await asyncio.to_thread(backend.write, payload)

    def _compose(self, command: str) -> bytes:
        """Build the bytes written to the shell: the command followed by a
        sentinel line carrying its exit code, on its own line."""
        marker_literal = self._marker.decode("latin-1")
        if self._shell_kind == "cmd":
            line = f"{command}\r\necho {marker_literal}%ERRORLEVEL%\r\n"
        else:
            line = f"{command}\nprintf '\\n{marker_literal}%d\\n' \"$?\"\n"
        return line.encode("utf-8")


def _partial_suffix_len(buf: bytearray, marker: bytes) -> int:
    """Length of the longest suffix of ``buf`` that is a prefix of ``marker``.

    Lets the demux retain a sentinel split across chunk boundaries without
    withholding ordinary output (which is not a marker prefix).
    """
    max_k = min(len(buf), len(marker))
    for k in range(max_k, 0, -1):
        if buf[len(buf) - k:] == marker[:k]:
            return k
    return 0


# ── Concrete backends ────────────────────────────────────────────────────────


class _UnixPtyBackend(_PtyBackend):
    """POSIX ``openpty`` backend: real TTY line discipline, echo disabled."""

    # Assigned in __init__; annotated here because that body is unreachable to a
    # type checker running on Windows (it begins with a win32 guard).
    _master: int
    _proc: "subprocess.Popen[bytes]"

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str]) -> None:
        if sys.platform == "win32":  # pragma: no cover - guarded by factory
            raise RuntimeError("unix pty backend unavailable on win32")
        import fcntl  # noqa: F401  (kept for parity / future non-block tuning)
        import termios

        master, slave = os.openpty()
        attrs = termios.tcgetattr(master)
        attrs[3] = attrs[3] & ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(master, termios.TCSANOW, attrs)
        self._master = master
        self._proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd or None,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid

    def read(self, size: int) -> bytes:
        return os.read(self._master, size)

    def write(self, data: bytes) -> None:
        os.write(self._master, data)

    def send_interrupt(self) -> None:
        if sys.platform == "win32":  # pragma: no cover - guarded by factory
            return
        os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)

    def terminate_tree(self) -> None:
        if sys.platform == "win32":  # pragma: no cover - guarded by factory
            return
        try:
            pgid = os.getpgid(self._proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=_GRACE_KILL_S)
                return
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def close(self) -> None:
        try:
            os.close(self._master)
        except OSError:
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None


class _WindowsPtyBackend(_PtyBackend):
    """ConPTY backend via ``pywinpty``. Reads are blocking → reader thread."""

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str]) -> None:
        import winpty  # type: ignore[import-not-found]

        self._pty = winpty.PtyProcess.spawn(argv, cwd=cwd or None, env=env or None)

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._pty, "pid", None)

    def read(self, size: int) -> bytes:
        data = self._pty.read(size)
        if data is None or data == "":
            return b""
        if isinstance(data, bytes):
            return data
        return data.encode("utf-8", errors="replace")

    def write(self, data: bytes) -> None:
        self._pty.write(data.decode("utf-8", errors="replace"))

    def send_interrupt(self) -> None:
        self.write(b"\x03")

    def terminate_tree(self) -> None:
        try:
            self._pty.kill()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return int(self._pty.wait(timeout))
        except Exception:  # noqa: BLE001 — pywinpty wait variants differ
            return None


class _PipeBackend(_PtyBackend):
    """Degraded transport: plain pipes, no TTY. Keeps the streaming contract."""

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str]) -> None:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd or None,
            env=env,
            bufsize=0,
            start_new_session=(sys.platform != "win32"),
            creationflags=creationflags,
        )

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid

    def read(self, size: int) -> bytes:
        stdout = self._proc.stdout
        if stdout is None:
            return b""
        return os.read(stdout.fileno(), size)

    def write(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        stdin.write(data)
        stdin.flush()

    def send_interrupt(self) -> None:
        if sys.platform == "win32":
            self._proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)

    def terminate_tree(self) -> None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=_GRACE_KILL_S)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def close(self) -> None:
        for stream in (self._proc.stdout, self._proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None


def _default_backend_factory(
    argv: List[str], cwd: str, env: Dict[str, str], marker: bytes,
) -> _PtyBackend:
    """Pick the strongest available transport for the host platform.

    ``marker`` is part of the contract for test doubles that emulate a shell;
    real backends run an actual shell that emits the sentinel and ignore it.
    """
    del marker  # real backends derive the sentinel from the live shell
    if sys.platform == "win32":
        try:
            return _WindowsPtyBackend(argv, cwd, env)
        except Exception as exc:  # noqa: BLE001 — pywinpty missing/broken → degrade
            logger.warning("pywinpty unavailable (%s) — using degraded pipe backend.", exc)
            return _PipeBackend(argv, cwd, env)
    try:
        return _UnixPtyBackend(argv, cwd, env)
    except OSError as exc:
        logger.warning("openpty unavailable (%s) — using degraded pipe backend.", exc)
        return _PipeBackend(argv, cwd, env)
