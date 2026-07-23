"""Tests for 11.3.B.1 — per-request latency telemetry (P50/P95).

Covers the hand-rolled percentile math, the writer/reader roundtrip, the additive
table+index migration, project scoping, the empty-window zero contract, and the
bounded ``recent`` payload.
"""
import sqlite3

import pytest

import core.telemetry as tele


# ── percentile math ──────────────────────────────────────────────────────────

def test_percentile_known_values() -> None:
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert tele._percentile(vals, 50) == pytest.approx(30.0)
    assert tele._percentile(vals, 95) == pytest.approx(48.0)  # 40 + 0.8*(50-40)
    assert tele._percentile(vals, 0) == pytest.approx(10.0)
    assert tele._percentile(vals, 100) == pytest.approx(50.0)


def test_percentile_single_and_empty() -> None:
    assert tele._percentile([42.0], 95) == pytest.approx(42.0)
    assert tele._percentile([], 50) == 0.0


# ── writer / reader roundtrip ────────────────────────────────────────────────

def test_latency_roundtrip_and_percentiles(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        for ms in (100.0, 200.0, 300.0, 400.0, 500.0):
            tele.log_request_latency("s1", "proj_A", ms, "completed")
        summary = tele.latency_percentiles()
    finally:
        tele.shutdown_telemetry_db()

    assert summary["count"] == 5
    assert summary["p50_ms"] == pytest.approx(300.0)
    assert summary["p95_ms"] == pytest.approx(480.0)
    assert summary["max_ms"] == pytest.approx(500.0)
    assert summary["avg_ms"] == pytest.approx(300.0)
    # recent is chronological (oldest → newest) for the sparkline
    assert summary["recent"] == [100.0, 200.0, 300.0, 400.0, 500.0]


def test_latency_project_filter(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        tele.log_request_latency("s1", "proj_A", 100.0, "completed")
        tele.log_request_latency("s2", "proj_B", 900.0, "completed")
        tele.log_request_latency("s3", "", 500.0, "failed")  # unscoped -> NULL
        scoped = tele.latency_percentiles(project_id="proj_A")
        every = tele.latency_percentiles()
    finally:
        tele.shutdown_telemetry_db()

    assert scoped["count"] == 1
    assert scoped["p50_ms"] == pytest.approx(100.0)
    assert every["count"] == 3


def test_latency_empty_returns_zeros(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        summary = tele.latency_percentiles()
    finally:
        tele.shutdown_telemetry_db()
    assert summary["count"] == 0
    assert summary["p50_ms"] == 0.0
    assert summary["p95_ms"] == 0.0
    assert summary["recent"] == []


def test_latency_recent_capped(tmp_path) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        for i in range(70):
            tele.log_request_latency("s", "proj_A", float(i), "completed")
        summary = tele.latency_percentiles()
    finally:
        tele.shutdown_telemetry_db()

    assert summary["count"] == 70
    # recent is capped to the newest 60, chronological
    assert len(summary["recent"]) == 60
    assert summary["recent"][0] == pytest.approx(10.0)
    assert summary["recent"][-1] == pytest.approx(69.0)


# ── migration ────────────────────────────────────────────────────────────────

def test_latency_migration_creates_table_and_index(tmp_path) -> None:
    db = str(tmp_path / "telemetry_old.sqlite")
    # A pre-existing DB that predates the request_latency table.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, session_id TEXT)"
    )
    conn.commit()
    conn.close()

    tele.init_telemetry_db(db)
    try:
        tele.log_request_latency("s1", "proj_A", 123.0, "completed")
        summary = tele.latency_percentiles(project_id="proj_A")
    finally:
        tele.shutdown_telemetry_db()

    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(request_latency)")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(request_latency)")}
    conn.close()

    assert "request_latency" in tables
    assert "idx_request_latency_project" in idx
    assert "project_id" in cols
    assert summary["count"] == 1
