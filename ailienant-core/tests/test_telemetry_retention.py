"""DEBT-120 — telemetry retention GC.

Covers `core.telemetry.purge_old_telemetry`: deletes rows older than the
retention window from the three append-only tables and leaves recent rows
untouched, no-ops cleanly when the DB isn't initialized, and is safe to call
concurrently from `core.janitor.run_janitor`/`purge_old_telemetry` (the async
wrapper offloading to a thread).
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import core.telemetry as tele
from core.janitor import TelemetryGCReport
from core.janitor import purge_old_telemetry as janitor_purge_old_telemetry


def _seed_row(table: str, timestamp: str, **extra: object) -> None:
    """Insert one row into `table` with an explicit (possibly backdated) timestamp."""
    assert tele._conn is not None
    if table == "request_latency":
        tele._conn.execute(
            "INSERT INTO request_latency (timestamp, session_id, project_id, duration_ms, outcome) "
            "VALUES (?, ?, ?, ?, ?)",
            (timestamp, extra.get("session_id", "s1"), extra.get("project_id"),
             extra.get("duration_ms", 100.0), extra.get("outcome", "success")),
        )
    elif table == "container_lifecycle":
        tele._conn.execute(
            "INSERT INTO container_lifecycle (timestamp, event, container_id, image, tier) "
            "VALUES (?, ?, ?, ?, ?)",
            (timestamp, extra.get("event", "started"), extra.get("container_id", "c1"),
             extra.get("image", "img"), extra.get("tier", "DOCKER")),
        )
    elif table == "action_token_usage":
        tele._conn.execute(
            "INSERT INTO action_token_usage (timestamp, action, total_tokens, project_id) "
            "VALUES (?, ?, ?, ?)",
            (timestamp, extra.get("action", "write_file"), extra.get("total_tokens", 500),
             extra.get("project_id")),
        )
    else:
        raise ValueError(table)
    tele._conn.commit()


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Iterator[None]:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    yield
    tele.shutdown_telemetry_db()


def test_purge_deletes_old_rows_keeps_recent(isolated_db: None) -> None:
    old = "2020-01-01 00:00:00"  # far past any retention window
    recent = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for table in ("request_latency", "container_lifecycle", "action_token_usage"):
        _seed_row(table, old)
        _seed_row(table, recent)

    deleted = tele.purge_old_telemetry(retention_days=30)

    assert deleted == {
        "request_latency": 1,
        "container_lifecycle": 1,
        "action_token_usage": 1,
    }
    assert tele._conn is not None
    for table in ("request_latency", "container_lifecycle", "action_token_usage"):
        remaining = tele._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert remaining == 1, f"{table} should retain exactly its recent row"


def test_purge_is_a_noop_when_db_not_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the uninitialized state explicitly rather than relying on another
    test's teardown order — `_conn` is a module global other test files touch too."""
    monkeypatch.setattr(tele, "_conn", None)
    deleted = tele.purge_old_telemetry()
    assert deleted == {
        "request_latency": 0,
        "container_lifecycle": 0,
        "action_token_usage": 0,
    }


@pytest.mark.anyio
async def test_janitor_purge_old_telemetry_wraps_the_sync_call(isolated_db: None) -> None:
    """`core.janitor.purge_old_telemetry` offloads to a thread and reshapes the
    dict into a typed `TelemetryGCReport` — the seam `run_janitor` calls."""
    OLD = "2020-01-01 00:00:00"
    _seed_row("request_latency", OLD)

    report = await janitor_purge_old_telemetry(retention_days=30)

    assert isinstance(report, TelemetryGCReport)
    assert report.request_latency_purged == 1
    assert report.container_lifecycle_purged == 0
    assert report.action_token_usage_purged == 0


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def test_lifespan_initializes_telemetry_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect: `init_telemetry_db()` was never called from `main.py`'s
    lifespan, so `core.telemetry._conn` stayed `None` for the whole process —
    every write silently no-op'd and the three REST endpoints backed by this
    store (/telemetry/routing, /telemetry/oom, /telemetry/latency) were
    permanently empty. `tests/conftest.py::_isolate_telemetry_db_path` points
    `main.TELEMETRY_DB_PATH` at a per-test temp file so this doesn't touch the
    real `~/.ailienant/telemetry.sqlite`.
    """
    import main

    # Force the pre-lifespan state explicitly — `_conn` is a module global
    # other test files also touch, so don't rely on execution order here.
    monkeypatch.setattr(tele, "_conn", None)
    with TestClient(main.app):
        assert tele._conn is not None, (
            "main.py's lifespan must call init_telemetry_db() at startup"
        )
    assert tele._conn is None, "lifespan shutdown must call shutdown_telemetry_db()"
