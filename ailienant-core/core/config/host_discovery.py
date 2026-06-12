"""Ephemeral host-discovery state for the External Capability Gateway.

The gateway is a separate, short-lived child process spawned by an external agent
(``python -m gateway``). It shares no memory with the running FastAPI host, so it
cannot know the host's loopback port or auth token a priori. The host therefore
writes an ephemeral state file at startup that the gateway reads to address the
loopback substrate.

The contract for that file lives here, in a single module used by both sides — the
host writes it, the gateway reads and probes it — so the shape can never drift.

Two safety properties are baked in:

* The file carries the loopback service token, which is god-tier on the local API.
  It is written ``0600`` (owner-only on POSIX; profile-scoped on Windows, where
  ``chmod`` only toggles the read-only bit) — the same posture as ``mcp_secrets``.
* The file is a *hint, not a truth*. A crash leaves it orphaned, pointing at a dead
  port, so a reader MUST probe liveness before trusting it. The probe's single
  source of truth is an async TCP connect to the recorded port — the only
  cross-platform, deterministic proof the host is actually listening.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("HOST_DISCOVERY")

# The host always binds the loopback interface; discovery never crosses machines.
_LOOPBACK_HOST = "127.0.0.1"

RUN_STATE_PATH: Path = Path.home() / ".ailienant" / "run.json"


class HostNotRunningError(RuntimeError):
    """Raised when the host state file is absent or fails its liveness probe."""


@dataclass(frozen=True)
class HostCoords:
    """Loopback coordinates the gateway needs to reach the host."""

    port: int
    token: Optional[str]
    pid: int


def write_run_state(port: int, token: Optional[str], pid: int) -> None:
    """Atomically write the run-state file with ``0600`` permissions.

    Mirrors the ``mcp_secrets`` write: ``mkstemp`` in the target directory, write,
    ``chmod`` owner-only, then ``os.replace`` for an atomic publish.
    """
    RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"port": port, "token": token, "pid": pid}, indent=2)
    fd, tmp = tempfile.mkstemp(dir=RUN_STATE_PATH.parent, prefix=".tmp_run_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, RUN_STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_run_state() -> None:
    """Best-effort removal of the run-state file on graceful shutdown.

    A clean shutdown leaves no file; a crash leaves a stale one that the liveness
    probe will reject.
    """
    try:
        RUN_STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 — cleanup must never raise on shutdown
        logger.warning("Could not remove %s: %s", RUN_STATE_PATH, exc)


def read_run_state() -> Optional[HostCoords]:
    """Parse the run-state file. Returns ``None`` if missing or malformed."""
    try:
        raw = RUN_STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", RUN_STATE_PATH, exc)
        return None
    try:
        data = json.loads(raw)
        return HostCoords(
            port=int(data["port"]),
            token=data.get("token"),
            pid=int(data["pid"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Malformed %s — ignoring: %s", RUN_STATE_PATH, exc)
        return None


async def probe_host_alive(coords: HostCoords, timeout_sec: float = 2.0) -> bool:
    """Return True only if the recorded host is actually listening.

    The TCP connect is the single source of truth: it is the one cross-platform,
    deterministic proof the FastAPI host is up on the recorded port. A recorded PID
    is unreliable (it can be reused after the host dies, or held in a zombie state
    by another handle), so it is used only as an optional fast-negative on POSIX —
    never as positive evidence.
    """
    # Fast-negative: on POSIX a missing PID means the host is definitely gone.
    # On Windows we have no cheap, reliable equivalent, so we skip straight to the
    # socket. A reused PID still passes here and is caught by the connect below.
    if os.name == "posix":
        try:
            os.kill(coords.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # alive but owned by another user — let the socket decide

    writer: Optional[asyncio.StreamWriter] = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(_LOOPBACK_HOST, coords.port),
            timeout=timeout_sec,
        )
        return True
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def resolve_host_or_error() -> HostCoords:
    """Read and verify the host coordinates, or raise ``HostNotRunningError``.

    Consumed by the gateway's EXECUTE-tier verbs, which need a live loopback target.
    """
    coords = read_run_state()
    if coords is None or not await probe_host_alive(coords):
        raise HostNotRunningError(
            "AILIENANT host is not running. Please open VS Code to start the engine."
        )
    return coords
