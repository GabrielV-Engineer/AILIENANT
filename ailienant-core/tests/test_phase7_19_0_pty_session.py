# tests/test_phase7_19_0_pty_session.py
"""Directed coverage for the persistent PTY session contract (core.pty_session).

A deterministic in-memory shell emulator (``_StubPtyBackend``) stands in for a
real PTY so the load-bearing behaviours are asserted without spawning a process
or depending on the host platform. Each DoD clause maps to one case:

  preserve cwd       two commands share the shell's working directory
  preserve env       two commands share an exported variable
  streamed deltas    output arrives as several chunks, not one final buffer
  stdin continues    a command parked on input resumes after write_stdin
  kill, no zombie    kill() tears down the tree and the reader thread is reaped
  loop not blocked    a hung command times out while the loop keeps progressing
  echo not boundary  an echoed sentinel is never mis-parsed as end-of-command
  lossless backpressure  a full bounded queue blocks the reader, drops nothing
  reader reaped      close() joins the reader thread
  partial utf-8      a multibyte char split across deltas reassembles cleanly

Unix-only variants exercise the real openpty backend (skipped on Windows).
"""
from __future__ import annotations

import asyncio
import codecs
import queue
import sys
from typing import Dict, List, Optional

import pytest

from core import pty_session
from core.pty_session import (
    SandboxSessionError,
    _PtyBackend,
    _PtySession,
)


# ── In-memory shell emulator ─────────────────────────────────────────────────


class _StubPtyBackend(_PtyBackend):
    """Deterministic shell emulator with its own cwd/env and a blocking read.

    Output is queued in small (<=8 byte) chunks so streaming is observable. A
    command never emits its sentinel until "finished"; ``hang`` never finishes,
    modelling a stuck process without blocking the event loop.
    """

    def __init__(self, marker: bytes, *, echo: bool = False) -> None:
        self._marker = marker
        self._echo = echo
        self._cwd = "/work"
        self._env: Dict[str, str] = {}
        self._chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._buffer = b""
        self._waiting_stdin = False
        self.terminated = False
        self.closed = False

    @property
    def pid(self) -> Optional[int]:
        return 4242

    def read(self, size: int) -> bytes:
        while not self._buffer:
            item = self._chunks.get()
            if item is None:
                return b""
            self._buffer += item
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def write(self, data: bytes) -> None:
        if self._waiting_stdin:
            self._waiting_stdin = False
            self._emit(b"got:" + data.rstrip(b"\r\n"))
            self._finish(0)
            return
        if self._echo:
            self._emit(data)
        self._dispatch(self._extract_command(data))

    def send_interrupt(self) -> None:
        self._emit(b"^C")

    def terminate_tree(self) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True
        self._chunks.put(None)

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return 0

    # ── emulator internals ───────────────────────────────────────────────────

    def _extract_command(self, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        idx = text.find("\nprintf '")
        if idx == -1:
            idx = text.find("\necho ")
        return (text[:idx] if idx != -1 else text).strip()

    def _dispatch(self, cmd: str) -> None:
        if cmd.startswith("cd "):
            self._cwd = cmd[3:].strip()
            self._finish(0)
        elif cmd == "pwd":
            self._emit(self._cwd.encode() + b"\n")
            self._finish(0)
        elif cmd.startswith("export "):
            key, _, value = cmd[len("export "):].partition("=")
            self._env[key.strip()] = value
            self._finish(0)
        elif cmd.startswith("echo $"):
            self._emit(self._env.get(cmd[len("echo $"):].strip(), "").encode() + b"\n")
            self._finish(0)
        elif cmd.startswith("echo "):
            self._emit(cmd[len("echo "):].encode() + b"\n")
            self._finish(0)
        elif cmd == "split":
            # 'x'*7 then a 2-byte é straddles the 8-byte chunk boundary.
            self._emit(b"xxxxxxx\xc3\xa9")
            self._finish(0)
        elif cmd == "boom":
            self._emit(b"kaboom\n")
            self._finish(7)
        elif cmd == "readline":
            self._waiting_stdin = True
        elif cmd == "hang":
            pass  # never finishes
        else:
            self._finish(0)

    def _emit(self, data: bytes) -> None:
        for i in range(0, len(data), 8):
            self._chunks.put(data[i:i + 8])

    def _finish(self, code: int) -> None:
        self._chunks.put(self._marker + str(code).encode() + b"\n")


def _make_session(
    *, echo: bool = False, guard: Optional[pty_session.PreSpawnGuard] = None,
) -> "tuple[_PtySession, Dict[str, _StubPtyBackend]]":
    holder: Dict[str, _StubPtyBackend] = {}

    def factory(
        argv: List[str], cwd: str, env: Dict[str, str], marker: bytes,
    ) -> _PtyBackend:
        backend = _StubPtyBackend(marker, echo=echo)
        holder["backend"] = backend
        return backend

    session = _PtySession(
        cwd="/work", env={}, shell_kind="posix",
        pre_spawn_guard=guard, backend_factory=factory,
    )
    return session, holder


async def _drain(session: _PtySession) -> bytes:
    out = bytearray()
    async for chunk in session.stream():
        out.extend(chunk)
    return bytes(out)


async def _drain_chunks(session: _PtySession) -> List[bytes]:
    chunks: List[bytes] = []
    async for chunk in session.stream():
        chunks.append(chunk)
    return chunks


# ── DoD cases ────────────────────────────────────────────────────────────────


def test_two_commands_preserve_cwd() -> None:
    async def body() -> "tuple[int, int, bytes]":
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        c1 = await session.run("cd /tmp", timeout_s=5)
        c2 = await session.run("pwd", timeout_s=5)
        await session.close()
        return c1, c2, await consumer

    c1, c2, out = asyncio.run(body())
    assert c1 == 0 and c2 == 0
    assert b"/tmp" in out


def test_two_commands_preserve_env() -> None:
    async def body() -> bytes:
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        await session.run("export FOO=bar", timeout_s=5)
        await session.run("echo $FOO", timeout_s=5)
        await session.close()
        return await consumer

    assert b"bar" in asyncio.run(body())


def test_output_arrives_in_multiple_deltas() -> None:
    async def body() -> List[bytes]:
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain_chunks(session))
        await session.run("echo " + "B" * 40, timeout_s=5)
        await session.close()
        return await consumer

    chunks = asyncio.run(body())
    assert len(chunks) >= 2
    assert b"B" * 40 in b"".join(chunks)


def test_write_stdin_unblocks_reader() -> None:
    async def body() -> "tuple[int, bytes]":
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        run_task = asyncio.ensure_future(session.run("readline", timeout_s=5))
        await asyncio.sleep(0.05)
        assert not run_task.done()  # parked awaiting stdin
        await session.write_stdin(b"hello\n")
        code = await asyncio.wait_for(run_task, timeout=5)
        await session.close()
        return code, await consumer

    code, out = asyncio.run(body())
    assert code == 0
    assert b"got:hello" in out


def test_kill_reaps_no_zombie() -> None:
    async def body() -> "tuple[bool, bool]":
        session, holder = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        hang = asyncio.ensure_future(session.run("hang", timeout_s=999))
        await asyncio.sleep(0.05)
        await session.kill()
        hang.cancel()
        await asyncio.gather(consumer, hang, return_exceptions=True)
        reader = session._reader
        reader_dead = reader is not None and not reader.is_alive()
        return holder["backend"].terminated, reader_dead

    terminated, reader_dead = asyncio.run(body())
    assert terminated
    assert reader_dead


def test_event_loop_not_blocked_under_hang() -> None:
    async def body() -> "tuple[bool, int]":
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        counter = {"n": 0}

        async def ticker() -> None:
            for _ in range(200):
                counter["n"] += 1
                await asyncio.sleep(0.005)

        tick = asyncio.ensure_future(ticker())
        raised = False
        try:
            await asyncio.wait_for(session.run("hang", timeout_s=999), timeout=0.3)
        except asyncio.TimeoutError:
            raised = True
        progressed = counter["n"]
        await session.close()
        tick.cancel()
        await asyncio.gather(consumer, tick, return_exceptions=True)
        return raised, progressed

    raised, progressed = asyncio.run(body())
    assert raised
    assert progressed > 0  # the loop kept running while the command hung


def test_echoed_command_not_treated_as_boundary() -> None:
    async def body() -> "tuple[int, bytes]":
        session, _ = _make_session(echo=True)
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        code = await session.run("boom", timeout_s=5)
        await session.close()
        return code, await consumer

    code, out = asyncio.run(body())
    assert code == 7            # resolved from the real sentinel, not the echo
    assert b"kaboom" in out
    assert b"printf" in out     # the written payload was echoed back


def test_full_queue_blocks_no_drop() -> None:
    async def body() -> bytes:
        original = pty_session._QUEUE_MAXSIZE
        pty_session._QUEUE_MAXSIZE = 2  # force backpressure
        try:
            session, _ = _make_session()
            await session.start()

            async def slow_drain() -> bytes:
                out = bytearray()
                async for chunk in session.stream():
                    out.extend(chunk)
                    await asyncio.sleep(0)  # yield so the queue fills
                return bytes(out)

            consumer = asyncio.ensure_future(slow_drain())
            await session.run("echo " + "A" * 1000, timeout_s=10)
            await session.close()
            return await consumer
        finally:
            pty_session._QUEUE_MAXSIZE = original

    out = asyncio.run(body())
    assert b"A" * 1000 in out   # every byte survived the bounded queue


def test_close_joins_reader_thread() -> None:
    async def body() -> bool:
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        await session.run("pwd", timeout_s=5)
        await session.close()
        await consumer
        reader = session._reader
        return reader is not None and not reader.is_alive()

    assert asyncio.run(body())


def test_partial_utf8_reassembles() -> None:
    async def body() -> List[bytes]:
        session, _ = _make_session()
        await session.start()
        consumer = asyncio.ensure_future(_drain_chunks(session))
        await session.run("split", timeout_s=5)
        await session.close()
        return await consumer

    chunks = asyncio.run(body())
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = "".join(decoder.decode(c) for c in chunks) + decoder.decode(b"", final=True)
    assert text == "xxxxxxxé"  # the split é reassembled, no UnicodeDecodeError


def test_pre_spawn_guard_vetoes_command() -> None:
    async def body() -> None:
        session, _ = _make_session(guard=lambda cmd: "blocked" if "rm" in cmd else None)
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        try:
            with pytest.raises(SandboxSessionError):
                await session.run("rm -rf /", timeout_s=5)
        finally:
            await session.close()
            await consumer

    asyncio.run(body())


# ── Unix-only real-backend variants ──────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="openpty is POSIX-only")
def test_real_unix_echo_disabled() -> None:
    async def body() -> bytes:
        session = _PtySession(cwd="", env={"PATH": "/usr/bin:/bin"}, shell_kind="posix")
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        await session.run("echo hi", timeout_s=10)
        await session.close()
        return await consumer

    out = asyncio.run(body())
    assert b"hi" in out
    assert b"echo hi" not in out  # the command line itself was not echoed


@pytest.mark.skipif(sys.platform == "win32", reason="killpg is POSIX-only")
def test_real_unix_kill_reaps_tree() -> None:
    import os

    async def body() -> Optional[int]:
        session = _PtySession(cwd="", env={"PATH": "/usr/bin:/bin"}, shell_kind="posix")
        await session.start()
        consumer = asyncio.ensure_future(_drain(session))
        hang = asyncio.ensure_future(session.run("sleep 30", timeout_s=999))
        await asyncio.sleep(0.2)
        pid = session._backend.pid if session._backend is not None else None
        await session.kill()
        hang.cancel()
        await asyncio.gather(consumer, hang, return_exceptions=True)
        return pid

    pid = asyncio.run(body())
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the shell process group is gone
