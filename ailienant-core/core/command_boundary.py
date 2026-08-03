# ailienant-core/core/command_boundary.py
"""Shared sentinel-marker command-boundary framing for interactive shell sessions.

Two independent :class:`~core.pty_session.SandboxSession` implementations run
the same protocol over different transports: the local PTY session
(``core.pty_session._PtySession``, a real pseudo-terminal driven by a reader
thread) and the devcontainer bridge session
(``api.devcontainer_bridge._BridgeSandboxSession``, a WebSocket tunnel with no
TTY and no reader thread). Both need to know where one command's output ends
and the next begins without a real TTY line discipline to lean on: a command
is followed by a shell-echoed sentinel line carrying its exit code, so the
byte stream can be split into "clean output" and "resolved exit code" purely
by pattern matching.

This module holds that transport-agnostic half of the protocol so it is
written — and tested — exactly once. Everything transport-specific (spawning,
reading, backpressure, teardown) stays local to each session implementation.
"""

from __future__ import annotations

import re
from typing import List, Tuple
from uuid import uuid4


class CommandBoundaryFramer:
    """Builds and parses one session's sentinel-marker command boundaries.

    A unique, control-char-prefixed marker per instance: control bytes
    0x01/0x02 do not appear in normal program output, and the resolved
    boundary line (marker + digits) never collides with the echoed command
    that emits it (which carries the format spec, not the resolved code).
    """

    def __init__(self, *, shell_kind: str) -> None:
        self._shell_kind = shell_kind
        self.marker: bytes = b"\x01\x02" + uuid4().hex.encode("ascii")
        self._boundary = re.compile(re.escape(self.marker) + rb"(\d+)\r?\n")

    def compose(self, command: str) -> bytes:
        """Build the bytes written to the shell: the command followed by a
        sentinel line carrying its exit code, on its own line."""
        marker_literal = self.marker.decode("latin-1")
        if self._shell_kind == "cmd":
            line = f"{command}\r\necho {marker_literal}%ERRORLEVEL%\r\n"
        else:
            line = f"{command}\nprintf '\\n{marker_literal}%d\\n' \"$?\"\n"
        return line.encode("utf-8")

    def drain_boundaries(self, buf: bytearray) -> Tuple[bytearray, bytearray, List[int]]:
        """Split completed-command sentinels out of ``buf``.

        Returns ``(bytes_to_emit, remaining_buf, resolved_codes)`` — one exit
        code per sentinel found, in encounter order (in practice at most one,
        since a session runs commands sequentially, but the caller decides how
        many pending futures to resolve, not this pure framing step). A
        trailing partial sentinel is retained in ``remaining_buf`` for the next
        chunk; everything else is emitted immediately so interactive prompts
        (which carry no newline) are never withheld.
        """
        emit = bytearray()
        codes: List[int] = []
        while True:
            match = self._boundary.search(buf)
            if match is None:
                keep = _partial_suffix_len(buf, self.marker)
                cut = len(buf) - keep
                emit.extend(buf[:cut])
                return emit, buf[cut:], codes
            emit.extend(buf[: match.start()])
            codes.append(int(match.group(1)))
            buf = bytearray(buf[match.end():])


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
