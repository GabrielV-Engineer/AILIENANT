# ailienant-core/core/exec_log.py
#
# Bounded, in-memory ring of recent sandbox command executions — the ephemeral
# sibling of the durable telemetry ledger. It answers a single operator
# question on the dashboard: "what commands is the agent running in the
# sandbox, and how did they exit?". Deliberately non-persistent: a live tail
# should not survive a restart, and keeping it in memory avoids write
# amplification (a task can exec dozens of times) and any retention burden.
#
# This module is also the SOLE masking site for the Glass-Box Timeline's
# execution-detail channel (server_activity_detail): record_execution feeds
# the turn-scoped ActivitySink (core/activity_context.py) the exact same
# masked, capped stdout/stderr this module already builds for the dashboard,
# so the WebSocket path can never diverge from the dashboard path on what is
# safe to show.

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Protocol, cast

from core.activity_context import current_activity_sink
from core.redaction import mask_secrets

if TYPE_CHECKING:  # avoid importing core.sandbox at runtime (keeps this leaf-light)
    from core.sandbox import SandboxResult


class _ExecAdapter(Protocol):
    """Structural type for anything ``record_execution`` can wrap.

    Deliberately narrower than ``core.sandbox.SandboxAdapter`` — the wrapper
    only needs ``execute`` — so this module stays a leaf and any conforming
    adapter (or test double) works without importing the concrete class.
    """

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> "SandboxResult": ...


logger = logging.getLogger("EXEC_LOG")

# Ring capacity. deque(maxlen=...) evicts the oldest entry in O(1) on overflow.
_RING_CAP: int = 200
# Per-field character budgets. Output can be arbitrarily large (npm install, a
# log dump), so we bound it before it ever enters the ring or the masker.
_OUTPUT_CAP: int = 2_000
_COMMAND_CAP: int = 1_000

_RING: Deque[Dict[str, object]] = deque(maxlen=_RING_CAP)
_seq: int = 0
_lock: threading.Lock = threading.Lock()


def _truncate_middle(text: str, cap: int) -> str:
    """Keep the head and tail of an over-long string, eliding the middle.

    Middle-truncation preserves both the command/prefix and the trailing error,
    which is where the useful signal usually sits. Cheap: the slices copy at
    most ``cap`` characters regardless of the source length.
    """
    if len(text) <= cap:
        return text
    half = cap // 2
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n…[{dropped} chars truncated]…\n{text[-half:]}"


def record_exec(
    source: str,
    session_id: str,
    command: str,
    result: "SandboxResult",
    duration_ms: float,
) -> Dict[str, object]:
    """Append one execution to the ring and return its masked, capped record.

    All bounding and secret-masking happens BEFORE the lock is taken; the
    critical section is only the sequence bump and the O(1) ``deque.append``,
    so a multi-megabyte stdout can never block readers or other writers.

    The ring entry keeps its pre-existing shape (a single combined ``output``
    field) so the dashboard's exec-log contract does not move. The returned
    dict is a SEPARATE object carrying ``stdout``/``stderr`` masked and capped
    independently (not sharing one combined budget), plus a ``truncated``
    flag — built for ``record_execution`` to hand to the Glass-Box Timeline
    detail sink without that sink re-deriving (and potentially diverging on)
    what is safe to show.

    Best-effort — never raises. On an internal fault returns ``{}``; the
    caller must treat that as "nothing to report", never as data.
    """
    global _seq
    try:
        raw_stdout = result.stdout or ""
        raw_stderr = result.stderr or ""
        combined = raw_stdout + raw_stderr
        safe_command = mask_secrets(_truncate_middle(command, _COMMAND_CAP)) or ""
        safe_output = mask_secrets(_truncate_middle(combined, _OUTPUT_CAP)) or ""
        safe_stdout = mask_secrets(_truncate_middle(raw_stdout, _OUTPUT_CAP)) or ""
        safe_stderr = mask_secrets(_truncate_middle(raw_stderr, _OUTPUT_CAP)) or ""
        truncated = (
            len(raw_stdout) > _OUTPUT_CAP
            or len(raw_stderr) > _OUTPUT_CAP
            or len(command) > _COMMAND_CAP
        )
        safe_duration_ms = round(float(duration_ms), 2)
        entry: Dict[str, object] = {
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "source": source,
            "command": safe_command,
            "exit_code": int(result.exit_code),
            "output": safe_output,
            "duration_ms": safe_duration_ms,
        }
        with _lock:
            _seq += 1
            entry["seq"] = _seq
            _RING.append(entry)
        return {
            "command": safe_command,
            "exit_code": int(result.exit_code),
            "stdout": safe_stdout,
            "stderr": safe_stderr,
            "duration_ms": safe_duration_ms,
            "truncated": truncated,
        }
    except Exception:  # noqa: BLE001 — observability must never affect the caller
        logger.debug("exec-log record skipped", exc_info=True)
        return {}


async def record_execution(
    adapter: _ExecAdapter,
    command: str,
    *,
    timeout_s: float,
    cwd: str,
    env_whitelist: Dict[str, str],
    session_id: Optional[str] = None,
    source: str,
) -> "SandboxResult":
    """Run ``adapter.execute`` and record the outcome to the ring.

    A thin pass-through the project-work call sites use in place of a direct
    ``adapter.execute(...)``. The execution itself is NOT wrapped in a
    swallow — a real infrastructure fault propagates exactly as before; only
    the recording is best-effort. Non-zero exit codes are ordinary returns and
    are captured.

    When a turn-scoped :class:`~core.activity_context.ActivitySink` is bound
    (``core/task_service.py`` binds one per coding turn; the dev-palette smoke
    path binds none — DEBT-122), this also drives the Glass-Box Timeline's
    execution-detail channel: a "command" marker fires before the call, keyed
    by a fresh execution id, and a matching detail fires after — on the
    success path from the masked record above, or on the fault path with
    ``error`` set and ``exit_code=None``. Emission is best-effort on both ends
    and never masks or replaces the real outcome: a raised fault is always
    re-raised after its detail is (attempted to be) reported.
    """
    sink = current_activity_sink()
    exec_id = uuid.uuid4().hex
    if sink is not None:
        try:
            await sink.emit_marker(ref=exec_id, target=command)
        except Exception:  # noqa: BLE001 — observability must never break the tool
            logger.debug("activity marker emit skipped (%s)", source, exc_info=True)

    t0 = time.perf_counter()
    try:
        result = await adapter.execute(
            command,
            timeout_s=timeout_s,
            cwd=cwd,
            env_whitelist=env_whitelist,
            session_id=session_id,
        )
    except Exception as exc:
        if sink is not None:
            try:
                await sink.emit_detail(
                    ref=exec_id,
                    source=getattr(adapter, "execution_source", "unknown"),
                    cwd=cwd,
                    initiator=source,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    truncated=False,
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001 — must never mask the real fault below
                logger.debug("activity detail emit skipped (fault, %s)", source, exc_info=True)
        raise

    duration_ms = (time.perf_counter() - t0) * 1000.0
    record: Dict[str, object] = {}
    try:
        record = record_exec(source, session_id or "", command, result, duration_ms)
    except Exception:  # noqa: BLE001 — observability must never break the tool
        logger.debug("exec-log emit skipped (%s)", source, exc_info=True)

    if sink is not None:
        try:
            await sink.emit_detail(
                ref=exec_id,
                source=getattr(adapter, "execution_source", "unknown"),
                cwd=cwd,
                initiator=source,
                stdout=cast(Optional[str], record.get("stdout")),
                stderr=cast(Optional[str], record.get("stderr")),
                exit_code=int(result.exit_code),
                duration_ms=duration_ms,
                truncated=bool(record.get("truncated", False)),
                error=None,
            )
        except Exception:  # noqa: BLE001 — observability must never break the tool
            logger.debug("activity detail emit skipped (%s)", source, exc_info=True)

    return result


def recent_exec_log(tail: int = 50, since: Optional[int] = None) -> Dict[str, object]:
    """Cursor-paged snapshot of the ring for the dashboard.

    Paging keys off the monotonic ``seq`` (tie-safe, unlike the display
    timestamp). With ``since`` set, returns only entries newer than that seq in
    chronological order — an idle poll then transfers next to nothing. Without
    it, returns the most recent ``tail`` entries. ``latest_seq`` lets the client
    advance its cursor. Never raises.
    """
    safe_tail = max(1, min(int(tail), _RING_CAP))
    with _lock:
        snapshot: List[Dict[str, object]] = list(_RING)
        latest_seq = _seq
    if since is not None:
        since_int = int(since)
        entries = [e for e in snapshot if cast(int, e.get("seq", 0)) > since_int]
        latest = max(since_int, latest_seq)
    else:
        entries = snapshot[-safe_tail:]
        latest = latest_seq
    return {"entries": entries, "latest_seq": latest}


def _reset_for_tests() -> None:
    """Clear the ring and sequence — test-only isolation helper."""
    global _seq
    with _lock:
        _RING.clear()
        _seq = 0
