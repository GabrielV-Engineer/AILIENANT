# ailienant-core/tests/test_e2e_resilience.py
# Phase 2.25.2 — Infinite loop failure mode: guardrail retry exhaustion + telemetry audit.
import sqlite3

import pytest

from brain.guardrails import MAX_RETRIES, route_after_validation, run_validate_output_node
from core.telemetry import init_telemetry_db, shutdown_telemetry_db

pytestmark = pytest.mark.anyio


@pytest.fixture()
def telemetry_db(tmp_path):
    db = tmp_path / "telemetry.sqlite"
    init_telemetry_db(db)
    yield db
    shutdown_telemetry_db()


def _bad_state(retry_count: int, session_id: str = "test") -> dict:
    """State with invalid vfs_buffer type to trigger CoderOutput validation failure."""
    return {
        "task_id": session_id,
        "retry_count": retry_count,
        "guardrail_failed": False,
        "validation_feedback": None,
        "vfs_buffer": "NOT_A_DICT",  # str → Pydantic ValidationError on Dict[str, Any]
        "current_step_id": 1,
        "target_role": "Refactor",
        "tci": 50.0,
        "css": 80.0,
    }


async def test_guardrail_fails_and_increments_on_first_error():
    """First bad output: guardrail_failed=True, retry_count becomes 1."""
    result = await run_validate_output_node(_bad_state(retry_count=0))
    assert result["guardrail_failed"] is True
    assert result["retry_count"] == 1


async def test_guardrail_exhaustion_exits_gracefully():
    """After MAX_RETRIES attempts, node returns guardrail_failed=False → graph reaches __end__."""
    result = await run_validate_output_node(_bad_state(retry_count=MAX_RETRIES))
    assert result["guardrail_failed"] is False, (
        f"Exhausted retries must exit cleanly "
        f"(guardrail_failed must be False after {MAX_RETRIES} retries)"
    )


async def test_retry_attempts_logged_in_telemetry(telemetry_db):
    """Each failed retry is audited in routing_decisions with reason containing 'guardrail_failed'."""
    session_id = "resilience-audit"
    for i in range(MAX_RETRIES):
        route_after_validation({
            "task_id": session_id,
            "guardrail_failed": True,
            "retry_count": i,
            "tci": 50.0,
            "css": 80.0,
        })

    conn = sqlite3.connect(str(telemetry_db))
    rows = conn.execute(
        "SELECT reason FROM routing_decisions "
        "WHERE session_id = ? AND source_node = 'validate_output'",
        (session_id,),
    ).fetchall()
    conn.close()

    assert len(rows) == MAX_RETRIES, (
        f"Expected {MAX_RETRIES} telemetry entries for retries, got {len(rows)}"
    )
    for (reason,) in rows:
        assert "guardrail_failed" in reason
