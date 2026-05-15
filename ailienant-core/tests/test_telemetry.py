# ailienant-core/tests/test_telemetry.py
#
# Phase 2.23 DoD: pytest tests/test_telemetry.py -v -> 0 failures.
#
# Coverage:
#   1. init_telemetry_db creates file and routing_decisions table
#   2. log_routing_decision writes a queryable record
#   3. log_routing_decision silently no-ops when DB not initialized
#   4. Integration: route_after_finops writes to the DB

import sqlite3

import pytest

import core.telemetry as tel
from brain.finops import route_after_finops


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Ensure each test starts and ends with a clean telemetry state."""
    tel.shutdown_telemetry_db()
    yield
    tel.shutdown_telemetry_db()


def test_init_creates_schema(tmp_path):
    """init_telemetry_db must create the file and routing_decisions table."""
    db = tmp_path / "telemetry.sqlite"
    tel.init_telemetry_db(db_path=db)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_decisions'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_log_routing_decision_writes_record(tmp_path):
    """log_routing_decision must INSERT a row that can be queried back."""
    db = tmp_path / "telemetry.sqlite"
    tel.init_telemetry_db(db_path=db)
    tel.log_routing_decision(
        session_id="sess-abc",
        source="planner_agent",
        target="coder_agent",
        reason="TCI exceeds threshold",
        css=75.0,
        tci=85.0,
        hw=None,
    )
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT session_id, source_node, target_node, reason, css_score, tci_score "
        "FROM routing_decisions"
    ).fetchone()
    conn.close()
    assert row == ("sess-abc", "planner_agent", "coder_agent", "TCI exceeds threshold", 75.0, 85.0)


def test_log_skips_when_not_initialized():
    """log_routing_decision must not raise when DB has not been initialized."""
    tel.log_routing_decision(
        session_id="nobody",
        source="x",
        target="y",
        reason="test",
    )


def test_integration_route_after_finops_logs(tmp_path):
    """route_after_finops must write a routing_decisions row when DB is live."""
    db = tmp_path / "telemetry.sqlite"
    tel.init_telemetry_db(db_path=db)
    state = {
        "task_id": "sess-integration",
        "hitl_response": None,
        "css": 80.0,
        "tci": 50.0,
    }
    result = route_after_finops(state)
    assert result == "apply_patch"
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT session_id, source_node, target_node FROM routing_decisions "
        "WHERE session_id='sess-integration'"
    ).fetchone()
    conn.close()
    assert row == ("sess-integration", "finops_gate", "apply_patch")
