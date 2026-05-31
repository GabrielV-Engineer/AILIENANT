# ailienant-core/core/telemetry_log.py
"""Live telemetry file sink — a tail-able audit surface for the Push bus.

Routing/OOM telemetry already persists to SQLite (``core/telemetry.py``); this
module adds a flat, ``tail -f``-friendly text sink at
``<workspace_root>/.ailienant_telemetry.log`` that records WS payloads, graph
node transitions and indexing events. It is the human/agent verification loop
for the event-driven Push features.

Concurrency contract — the sink is called from coroutines running on the FastAPI
asyncio loop (``send_personal_message`` / ``validate_incoming``). A synchronous
file write on that thread would stall the whole WebSocket server, so the logger
owns a :class:`~logging.handlers.QueueHandler` (an O(1), non-blocking enqueue)
and a background :class:`~logging.handlers.QueueListener` thread performs the
actual rotating disk write off-loop. Enqueue happens on the calling thread;
blocking I/O never does.

Security — the file is an audit surface holding code snippets and prompts. The
:class:`SecretsScrubberFilter` is attached to the *queue* handler so redaction
runs on the calling thread before a record is enqueued: plaintext secrets never
enter the in-memory queue, let alone the file.

Robustness — the queue is bounded (a flood is shed, never OOMs), each line is
size-capped, the rotating handler is itself size-bounded, and every public
entry point swallows its own errors so telemetry can never break a caller.
"""
from __future__ import annotations

import logging
import queue
import threading
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from shared.logging_filters import SecretsScrubberFilter

_LOG_FILENAME: str = ".ailienant_telemetry.log"
_MAX_BYTES: int = 5 * 1024 * 1024  # hard per-file disk cap
_BACKUP_COUNT: int = 3
_LINE_CAP: int = 2_000             # per-record truncation — one payload can't bloat a line
_QUEUE_MAX: int = 10_000           # bounded: a runaway flood is shed, never OOMs the loop

# Bounded queue — when saturated, QueueHandler.enqueue raises queue.Full, whose
# error path drops the record (telemetry is droppable). Upstream the 7.13.1
# inbound token bucket already throttles, so saturation is a last-resort guard.
_log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=_QUEUE_MAX)

logger: logging.Logger = logging.getLogger("AILIENANT_TELEMETRY")
logger.propagate = False  # never double-write to root/console
logger.setLevel(logging.INFO)

_queue_handler: QueueHandler = QueueHandler(_log_queue)
# Scrub on the calling thread, pre-enqueue: plaintext secrets never enter the queue.
_queue_handler.addFilter(SecretsScrubberFilter())
logger.addHandler(_queue_handler)

_lock: threading.Lock = threading.Lock()
_listener: Optional[QueueListener] = None
_file_handler: Optional[RotatingFileHandler] = None
_configured_root: Optional[str] = None  # the workspace_root the listener targets


def configure_telemetry_log(workspace_root: str) -> None:
    """Point the sink at ``<workspace_root>/.ailienant_telemetry.log``.

    Idempotent — a no-op if already targeting ``workspace_root`` or if the path
    is empty. Starts (or restarts) the background listener that drains the queue
    to a rotating, UTF-8 file handler. Safe to call on every client connect.
    """
    global _listener, _file_handler, _configured_root
    if not workspace_root:
        return
    with _lock:
        if workspace_root == _configured_root:
            return
        _stop_listener_locked()
        try:
            target = Path(workspace_root) / _LOG_FILENAME
            handler = RotatingFileHandler(
                str(target),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",  # survive emoji / non-ASCII paths under cp1252
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            listener = QueueListener(_log_queue, handler, respect_handler_level=True)
            listener.start()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "telemetry_log: could not open sink at %s — %s", workspace_root, exc
            )
            return
        _file_handler = handler
        _listener = listener
        _configured_root = workspace_root


def shutdown_telemetry_log() -> None:
    """Stop the background listener (drain + join) and close the file handler."""
    global _configured_root
    with _lock:
        _stop_listener_locked()
        # Discard any records enqueued while no listener was draining, so they can
        # never spill into a sink configured later for a different target.
        while True:
            try:
                _log_queue.get_nowait()
            except queue.Empty:
                break
        _configured_root = None  # reset target so a later configure() re-arms


def _stop_listener_locked() -> None:
    """Tear down the active listener/handler. Caller must hold ``_lock``."""
    global _listener, _file_handler
    if _listener is not None:
        try:
            _listener.stop()  # drains the queue then joins the worker thread
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
        _listener = None
    if _file_handler is not None:
        try:
            _file_handler.close()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
        _file_handler = None


def _emit(category: str, fields: Dict[str, Any]) -> None:
    """Format one capped line and enqueue it. Never raises into a caller."""
    try:
        parts = [category] + [f"{k}={v}" for k, v in fields.items()]
        line = " | ".join(parts)
        if len(line) > _LINE_CAP:
            line = line[:_LINE_CAP] + "…"
        # Lazy %-arg so the SecretsScrubberFilter processes the value pre-format.
        logger.info("%s", line)
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        pass


def log_ws_payload(
    direction: str, event_type: str, session_id: str, summary: str
) -> None:
    """Record one WebSocket event (``direction`` is ``"in"`` or ``"out"``)."""
    _emit(
        "WS",
        {"dir": direction, "event": event_type, "session": session_id, "data": summary},
    )


def log_node_transition(
    session_id: str, source: str, target: str, reason: str
) -> None:
    """Record a LangGraph node transition (mirrors the SQLite routing audit)."""
    _emit(
        "NODE",
        {"session": session_id, "from": source, "to": target, "reason": reason},
    )


def log_indexing_event(
    session_id: str, action: str, filepath: str, detail: str = ""
) -> None:
    """Record a GraphRAG indexing event (upsert / purge / migrate)."""
    _emit(
        "INDEX",
        {"session": session_id, "action": action, "file": filepath, "detail": detail},
    )
