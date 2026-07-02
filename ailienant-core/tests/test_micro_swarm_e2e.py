# ailienant-core/tests/test_micro_swarm_e2e.py
# Phase 2.25.3 — Micro-swarm E2E: Vigilia + polyglot detection + SWARM telemetry audit.
import json
import sqlite3

import pytest

from brain.engine import route_to_coders
from brain.state import MissionSpecification, WBSStep
from core.rules import rule_manager
from core.telemetry import init_telemetry_db, shutdown_telemetry_db
from core.utils import is_polyglot_file


@pytest.fixture(autouse=True)
def reset_rules():
    rule_manager.reset()
    yield
    rule_manager.reset()


@pytest.fixture()
def telemetry_db(tmp_path):
    db = tmp_path / "tel.sqlite"
    init_telemetry_db(db)
    yield db
    shutdown_telemetry_db()


def _swarm_state(session_id: str, tasks: list) -> dict:
    """Minimal AIlienantGraphState dict for CLOUD SWARM routing."""
    mission = MissionSpecification(
        outcome="Checkpoint swarm test.",
        scope=["main.py"],
        constraints=[],
        decisions=[],
        tasks=tasks,
        checks=[],
    )
    return {
        "task_id": session_id,
        "provider": "CLOUD",
        "parallel_tasks": tasks,
        "mission_spec": mission,
        "tci": 90.0,
        "css": 75.0,
        "user_input": "",
        "project_id": None,
        "workspace_root": "",
        "explicit_mentions": [],
        "attachments": [],
        "messages": [],
        "is_manual_override": False,
        "target_role": None,
        "current_step_id": None,
        "planner_mode_active": False,
        "hitl_pending": False,
        "hitl_response": None,
        "shared_understanding_reached": False,
        "read_files_state": {},
        "vfs_buffer": {},
        "has_images": False,
        "routing_warning": None,
        "hardware_profile": None,
        "generated_code": {},
        "errors": [],
        "retry_count": 0,
        "security_flags": [],
        "terminal_output": "",
        "session_delta": "",
        "is_indexing_complete": True,
        "guardrail_failed": False,
        "validation_feedback": None,
        "immutable_wbs": None,
        "pending_patches": {},
        "current_cost_usd": 0.0,
        "max_budget_usd": float("inf"),
        "context_metrics": None,
        "active_llm_profile": None,
        "token_usage": None,
    }


def test_vigilia_rule_manager_hit_in_swarm_workspace(tmp_path):
    """RuleManager loads .ailienant.json from a Vigilia-protected workspace correctly."""
    (tmp_path / ".ailienant.json").write_text(
        json.dumps({"rules": ["No global installs", "Use pydantic models"]})
    )
    result = rule_manager.get_combined_rules(str(tmp_path))
    assert "No global installs" in result
    assert "Use pydantic models" in result


def test_polyglot_detection_triggered_for_mixed_syntax_files():
    """is_polyglot_file flags files requiring patch_file tool and leaves pure Python clear."""
    assert is_polyglot_file("frontend/App.vue") is True
    assert is_polyglot_file("template.jinja2") is True
    assert is_polyglot_file("admin.blade.php") is True
    assert is_polyglot_file("src/main.py") is False


def test_swarm_routing_logs_cloud_swarm_decision(telemetry_db):
    """route_to_coders with CLOUD + parallel tasks emits 2 Sends and logs 'SWARM' in telemetry."""
    tasks = [
        WBSStep(
            step_number=1,
            target_role="Refactor",
            action="read_file",
            target_file="main.py",
            description="Read main.",
            status="pending",
        ),
        WBSStep(
            step_number=2,
            target_role="Test",
            action="read_file",
            target_file="requirements.txt",
            description="Audit deps.",
            status="pending",
        ),
    ]
    sends = route_to_coders(_swarm_state("swarm-e2e", tasks))  # pyright: ignore[reportArgumentType] — dict test double for the AIlienantGraphState TypedDict

    assert len(sends) == 2, "CLOUD SWARM must fan-out one Send per parallel task"

    conn = sqlite3.connect(str(telemetry_db))
    rows = conn.execute(
        "SELECT reason FROM routing_decisions WHERE session_id = 'swarm-e2e'",
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    reason = rows[0][0]
    assert "SWARM" in reason
    assert "CLOUD" in reason
