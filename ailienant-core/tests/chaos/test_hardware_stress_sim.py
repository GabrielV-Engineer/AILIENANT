"""Hardware Stress Simulator — chaos-engineering of graceful degradation.

Applies synthetic memory pressure by injecting a starved HardwareProfile (rather
than allocating real RAM/VRAM, which would be non-deterministic and could OOM the
test host) and asserts the routing engine degrades a LOCAL decision and that the
fallback is observable in the telemetry sink. The real-allocation variant is
deferred (DEBT-067).
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import ContextMeter, LLMProfile, MissionSpecification, WBSStep
from core.memory.context_auditor import RiskLevel
from core.telemetry import init_telemetry_db, shutdown_telemetry_db
from shared.hardware import HardwareDetector, HardwareProfile

pytestmark = pytest.mark.anyio


def _starved_profile() -> HardwareProfile:
    """A host whose effective VRAM sits far below the cloud floor."""
    return HardwareProfile(
        os_type="windows", is_apple_silicon=False,
        vram_gb=8.0, vram_used_gb=7.8,  # 0.2 GB free → below the 4 GB floor
    )


def _mission() -> MissionSpecification:
    return MissionSpecification(
        outcome="done", scope=["a.py"], constraints=[], decisions=[],
        tasks=[WBSStep(step_number=1, target_role="architect_refactor",
                       action="read_file", target_file="a.py",
                       description="d", status="pending")],
        checks=["c"],
    )


def _state(profile: HardwareProfile) -> Dict[str, Any]:
    ctx = ContextMeter(
        semantic_similarity=0.9, graph_coverage=0.9, recency_score=0.4,
        css_total=100.0, task_complexity_index=10.0,
        routing_decision="LOCAL_SMALL", is_red_alert=False,
    )
    return {
        "task_id": "chaos-vram",
        "user_input": "refactor the authentication module",
        "workspace_root": "/tmp/ws_chaos",
        "project_id": "chaos",
        "context_metrics": ctx,
        "mission_spec": None, "immutable_wbs": None, "errors": [],
        "retry_count": 0, "current_cost_usd": 0.0, "max_budget_usd": 10.0,
        "vfs_buffer": {}, "terminal_output": "", "parallel_tasks": [],
        "tci": 10.0, "css": 100.0, "provider": "LOCAL",
        "current_step_id": None, "dirty_buffers": [], "ide_context": "",
        "hardware_profile": profile,
        "active_llm_profile": LLMProfile(
            model_name="ailienant/small", parameters_b=1.5,
            context_window=8192, quantization="q4_0"),
    }


async def test_synthetic_vram_pressure_triggers_observable_fallback(tmp_path: Any) -> None:
    db = tmp_path / "telemetry.sqlite"
    init_telemetry_db(db)
    try:
        # Synthetic pressure: the detector now reports a starved host.
        with patch.object(HardwareDetector, "detect", return_value=_starved_profile()):
            starved = HardwareDetector.detect()
            assert starved.suggested_mode != "FULL_SWARM"  # pressure is real to the detector

        search = AsyncMock(return_value=(0.9, ["a.py"], [""]))
        deep = AsyncMock(return_value=MagicMock(
            coverage_ratio=0.9, context_block="", parsed_files=["a.py"], target_files=["a.py"]))
        audit = AsyncMock(return_value=RiskLevel.NONE)
        llm = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=_mission().model_dump_json()))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)))

        with patch("agents.planner.DEBUG_MODE", False), \
             patch("core.state_manager.load_state_from_markdown", return_value=None), \
             patch("core.state_manager.dump_state_to_markdown", return_value=True), \
             patch("agents.planner.audit_task_complexity", new=audit), \
             patch("agents.planner.check_cloud_availability", return_value=True), \
             patch("agents.planner.SemanticMemoryManager") as sem_cls, \
             patch("agents.planner.GraphRAGDynamicExtractor") as extr_cls, \
             patch("agents.planner.TrajectoryMemoryManager") as traj_cls, \
             patch("agents.planner.LLMGateway.ainvoke", new=llm):
            traj_cls.return_value.search = AsyncMock(return_value=[])
            extr_cls.return_value.deep_parse = deep
            sem_cls.return_value.search_with_paths = search

            from agents.planner import run_planner_node
            result = await run_planner_node(_state(_starved_profile()))

        # Fallback fired: LOCAL decision degraded to CLOUD with a user-facing warning.
        assert result["context_metrics"].routing_decision == "CLOUD"
        assert result["routing_warning"] is not None

        # Observable in telemetry: a vram_floor_reroute row was recorded.
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT reason, target_node FROM routing_decisions WHERE session_id = ?",
            ("chaos-vram",),
        ).fetchall()
        conn.close()
        assert any(reason == "vram_floor_reroute" and target == "CLOUD"
                   for reason, target in rows), rows
    finally:
        shutdown_telemetry_db()
