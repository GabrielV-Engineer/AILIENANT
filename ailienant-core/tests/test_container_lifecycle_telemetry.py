"""Tests for 11.3.B.2 — Docker container lifecycle telemetry.

Covers the writer/reader roundtrip (machine-global, newest-first, clamped), the
additive table creation, and that the sandbox adapter's lifecycle wrappers emit
events — verified with a fake container, without spinning real Docker.
"""
import asyncio
import sqlite3

import core.telemetry as tele
from core.sandbox import DockerSandboxAdapter


# ── writer / reader ──────────────────────────────────────────────────────────

def test_lifecycle_roundtrip_and_order(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        tele.log_container_event("started", "abc123", "ailienant-sandbox:latest", "DOCKER")
        tele.log_container_event("stopped", "abc123", "ailienant-sandbox:latest", "DOCKER")
        events = tele.recent_container_events()
    finally:
        tele.shutdown_telemetry_db()

    assert len(events) == 2
    assert events[0]["event"] == "stopped"  # newest first
    assert events[1]["event"] == "started"
    assert events[0]["container_id"] == "abc123"
    assert events[0]["tier"] == "DOCKER"


def test_lifecycle_limit_clamp(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        for i in range(5):
            tele.log_container_event("started", f"c{i}", "img", "DOCKER")
        events = tele.recent_container_events(limit=2)
    finally:
        tele.shutdown_telemetry_db()
    assert len(events) == 2


def test_lifecycle_table_created(tmp_path) -> None:
    db = str(tmp_path / "telemetry_old.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.commit()
    conn.close()

    tele.init_telemetry_db(db)
    try:
        tele.log_container_event("started", "x", "img", "DOCKER")
        events = tele.recent_container_events()
    finally:
        tele.shutdown_telemetry_db()

    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "container_lifecycle" in tables
    assert len(events) == 1


# ── sandbox emission (hermetic — no real Docker) ─────────────────────────────

class _FakeContainer:
    id = "deadbeefcafe0000"

    def stop(self, timeout: int = 10) -> None:  # noqa: D401 - fake
        pass

    def remove(self, force: bool = False) -> None:  # noqa: D401 - fake
        pass


def test_emit_lifecycle_calls_writer(tmp_path, monkeypatch) -> None:
    captured: list = []
    monkeypatch.setattr(
        tele, "log_container_event",
        lambda event, cid, image, tier: captured.append((event, cid, image, tier)),
    )
    adapter = DockerSandboxAdapter(host_workspace=str(tmp_path))
    adapter._emit_lifecycle("started", _FakeContainer())

    assert len(captured) == 1
    event, cid, _image, tier = captured[0]
    assert event == "started"
    assert cid == "deadbeefcafe"  # truncated to 12 chars
    assert tier == "DOCKER"


def test_shutdown_emits_stopped(tmp_path, monkeypatch) -> None:
    captured: list = []
    adapter = DockerSandboxAdapter(host_workspace=str(tmp_path))
    adapter._container = _FakeContainer()
    monkeypatch.setattr(adapter, "_emit_lifecycle", lambda event, container=None: captured.append(event))

    asyncio.run(adapter.shutdown())

    assert "stopped" in captured
    assert adapter._container is None  # teardown completed
