"""Durable per-caller DoS guard for the External Capability Gateway.

The gateway is an ephemeral stdio child process: an in-memory rate limiter would
reset to zero on every reconnect, so an attacker or a looping agent could defeat it
by simply restarting the connection. The ledger is therefore a *persistent* security
control — a JSON store, keyed by caller, that survives process restarts.

It enforces two axes per caller:

* a **token-bucket rate limiter** on call frequency, and
* a **cumulative budget** ceiling (wired to real token cost by the EXECUTE verbs).

Concurrency is guarded by a ``filelock`` on a *dedicated* ``.lock`` file — never the
data file itself. Locking the data file would break mutual exclusion the instant an
atomic ``os.replace`` swaps the data inode out from under the held lock, letting a
concurrent process lock the new inode and interleave writes.

The rate math is hardened against wall-clock regressions (NTP adjustments, manual
clock changes): the elapsed delta is floored at zero and the refilled total is capped
at the bucket size, so a backward clock can never subtract tokens or overflow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import stat
import tempfile
import time
from typing import Dict

from filelock import FileLock, Timeout

from shared.config import DB_CATALOG_PATH

logger = logging.getLogger("GATEWAY_LEDGER")

# Co-locate with the catalog DB, identical derivation to mcp_secrets.
_CATALOG_PATH: pathlib.Path = pathlib.Path(DB_CATALOG_PATH).resolve()
LEDGER_PATH: pathlib.Path = _CATALOG_PATH.parent / "gateway_ledger.json"

# Held only briefly per transaction; a security control fails CLOSED on contention.
_LOCK_TIMEOUT_S: float = 5.0

_CallerRecord = Dict[str, float]
_Store = Dict[str, _CallerRecord]


def _lock_path() -> pathlib.Path:
    """The dedicated lock file, derived from LEDGER_PATH at call time.

    Computed from the current LEDGER_PATH so a test that monkeypatches the data
    path moves the lock with it.
    """
    return pathlib.Path(str(LEDGER_PATH) + ".lock")


def _rate_cap() -> float:
    return float(os.environ.get("AILIENANT_GATEWAY_RATE_CAP", "60"))


def _refill_per_s() -> float:
    return float(os.environ.get("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "1.0"))


def _budget_ceiling() -> float:
    return float(os.environ.get("AILIENANT_GATEWAY_BUDGET", "1000000"))


def _load() -> _Store:
    """Read the whole ledger. Returns ``{}`` on a missing or corrupt file.

    Atomic writes mean a lockless reader always sees a complete old-or-new file.
    """
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001 — corrupt store ≠ crash
        logger.warning("Invalid %s — treating as empty: %s", LEDGER_PATH, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    store: _Store = {}
    for caller_id, rec in data.items():
        if isinstance(rec, dict):
            store[str(caller_id)] = {
                str(k): float(v)
                for k, v in rec.items()
                if isinstance(v, (int, float))
            }
    return store


def _save(store: _Store) -> None:
    """Atomic + 0600 + UTF-8 write of the whole ledger."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(store, indent=2)
    fd, tmp = tempfile.mkstemp(dir=LEDGER_PATH.parent, prefix=".tmp_gwledger_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rate_txn(caller_id: str) -> bool:
    """Locked read-modify-write of one caller's token bucket. True if a token was
    consumed; False if the bucket was empty or the lock could not be acquired."""
    lock = FileLock(str(_lock_path()), timeout=_LOCK_TIMEOUT_S)
    try:
        lock.acquire()
    except Timeout:
        # Fail closed: an attacker must not bypass the guard by holding the lock.
        logger.warning("gateway ledger lock busy — denying %s (fail-closed)", caller_id)
        return False
    try:
        store = _load()
        rec = store.get(caller_id, {})
        cap = _rate_cap()
        now = time.time()
        last = rec.get("refill_at", now)
        # Clock-skew safe: a backward wall clock never subtracts tokens or overflows.
        elapsed = max(0.0, now - last)
        tokens = min(cap, rec.get("bucket_tokens", cap) + elapsed * _refill_per_s())
        granted = tokens >= 1.0
        rec["bucket_tokens"] = tokens - 1.0 if granted else tokens
        rec["refill_at"] = now
        rec.setdefault("budget_consumed", 0.0)
        store[caller_id] = rec
        _save(store)
        return granted
    finally:
        lock.release()


def _budget_consume_txn(caller_id: str, amount: float) -> None:
    lock = FileLock(str(_lock_path()), timeout=_LOCK_TIMEOUT_S)
    try:
        lock.acquire()
    except Timeout:
        logger.warning("gateway ledger lock busy — budget for %s not recorded", caller_id)
        return
    try:
        store = _load()
        rec = store.get(caller_id, {})
        # Floor at zero so a refund (a negative amount) can never drive a caller's
        # cumulative budget below zero and gift them free headroom.
        rec["budget_consumed"] = max(0.0, rec.get("budget_consumed", 0.0) + amount)
        rec.setdefault("bucket_tokens", _rate_cap())
        rec.setdefault("refill_at", time.time())
        store[caller_id] = rec
        _save(store)
    finally:
        lock.release()


async def check_and_consume_rate(caller_id: str) -> bool:
    """Consume one rate token for this caller. False when throttled or lock-busy."""
    return await asyncio.to_thread(_rate_txn, caller_id)


async def consume_budget(caller_id: str, amount: float) -> None:
    """Add ``amount`` to a caller's cumulative budget (persisted)."""
    await asyncio.to_thread(_budget_consume_txn, caller_id, float(amount))


async def budget_exceeded(caller_id: str) -> bool:
    """True when a caller's cumulative budget has reached its ceiling."""
    store = await asyncio.to_thread(_load)
    consumed = store.get(caller_id, {}).get("budget_consumed", 0.0)
    return consumed >= _budget_ceiling()
