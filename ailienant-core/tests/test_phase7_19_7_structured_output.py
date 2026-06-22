"""Phase 7.19.7 DoD — structured agent output.

  A. Early plan emit: the moment a graph node produces the WBS, the plan document
     is broadcast (seed-only, empty summary) BEFORE the turn-end broadcast, so the
     chat can seed the execution checklist ahead of the per-step status mutations.
  B. WBS seeding: the planner instruction carries the directive that tells the
     model to honor a user-provided enumerated list as the WBS seed.

Reuses the established planner mock harness (test_planner.py) and the graph-run
harness (test_engine_respine.py).
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.planner import _WBS_SEED_DIRECTIVE
from brain.state import MissionSpecification, WBSStep
from core.response_cache import response_cache
from core.task_service import TaskService, TaskPayload


# ════════════════════════════════════════════════════════════════════════════
# B — planner WBS-seed directive
# ════════════════════════════════════════════════════════════════════════════


def test_wbs_seed_directive_content() -> None:
    text = _WBS_SEED_DIRECTIVE
    assert "WBS seed" in text
    assert "enumerated" in text
    # It must permit refinement but forbid discarding the user's list.
    assert "refine" in text
    assert "honor the user's structure" in text


def _valid_mission_json() -> str:
    return MissionSpecification(
        outcome="Test outcome.",
        scope=["test/scope.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="architect_refactor",
                action="read_file",
                target_file="test/scope.py",
                description="Stub task.",
            )
        ],
        checks=["Pytest exits 0."],
    ).model_dump_json()


def _make_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


def _broker_decision() -> MagicMock:
    decision = MagicMock()
    decision.cancelled = False
    decision.effective_model = "ailienant/big"
    decision.holds_lock = False
    return decision


def _planner_state(user_input: str) -> Dict[str, Any]:
    return {
        "task_id": "planner-seed-test",
        "user_input": user_input,
        "workspace_root": "/ws",
        "project_id": "abc123",
        "context_metrics": None,
        "mission_spec": None,
        "immutable_wbs": None,
        "errors": [],
        "retry_count": 0,
        "current_cost_usd": 0.0,
        "max_budget_usd": 10.0,
        "vfs_buffer": {},
        "terminal_output": "",
        "parallel_tasks": [],
        "tci": 45.0,
        "css": 78.5,
        "provider": "LOCAL",
        "current_step_id": None,
        "dirty_buffers": [],
        "ide_context": "",
        "researcher_skeleton": None,
    }


@pytest.fixture(autouse=True)
def _reset_response_cache() -> Any:
    response_cache.clear()
    yield
    response_cache.clear()


@pytest.mark.anyio
async def test_planner_instruction_carries_wbs_seed_directive() -> None:
    """A user prompt that is an enumerated list must reach the model with the
    WBS-seed directive in the instruction, so it honors the user's plan."""
    user_input = "1. Read config.py\n2. Add a logger\n3. Run pytest"
    mock_ainvoke = AsyncMock(return_value=_make_response(_valid_mission_json()))
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    # The Planner is a pure WBS engine now; it no longer performs retrieval/cascade,
    # so it consumes context_metrics from state instead of computing it.
    with patch("agents.planner.DEBUG_MODE", False), patch(
        "agents.planner.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "agents.planner.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(_planner_state(user_input))

    assert result.get("mission_spec") is not None
    instruction: str = mock_ainvoke.call_args_list[0].kwargs["messages"][-1]["content"]
    assert _WBS_SEED_DIRECTIVE in instruction


# ════════════════════════════════════════════════════════════════════════════
# A — early plan emit
# ════════════════════════════════════════════════════════════════════════════


_MISSION = MissionSpecification(
    outcome="Add a CSV exporter.",
    scope=["export.py"],
    constraints=["none"],
    decisions=["go"],
    tasks=[
        WBSStep(
            step_number=1,
            target_role="core_dev",
            action="edit_file",
            target_file="export.py",
            description="write exporter",
        )
    ],
    checks=["ok"],
)

_FINAL_WITH_PATCH: Dict[str, Any] = {
    "mission_spec": _MISSION,
    "pending_patches": {"export.py": "--- a/export.py\n+++ b/export.py\n"},
    "pending_contents": {"export.py": "def to_csv():\n    return ''\n"},
    "pending_base_hash": {"export.py": "cafef00d"},
    "errors": [],
    "hitl_pending": False,
}


def _astream_final(state: Dict[str, Any]) -> Any:
    def _maker(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield state

        return _gen()

    return _maker


def _payload() -> TaskPayload:
    return TaskPayload(
        task_prompt="build a CSV exporter",
        dirty_buffers=[],
        explicit_mentions=[],
        attachments=[],
        planner_mode_active=False,
        workspace_root="",
    )


@pytest.mark.anyio
async def test_early_plan_emit_precedes_turn_end_broadcast() -> None:
    """The first snapshot carrying a mission_spec triggers a seed-only plan
    broadcast (empty summary), ahead of the turn-end broadcast (real summary)."""
    ts = TaskService()  # type: ignore[no-untyped-call]
    broadcast_plan = AsyncMock()
    request_human_approval = AsyncMock(
        return_value={"approved": True, "comment": None, "modified_content": None}
    )
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["export.py"]})

    with patch("brain.engine.alienant_app.astream", side_effect=_astream_final(_FINAL_WITH_PATCH)), \
         patch("core.write_pipeline.apply_patch_set", apply_mock), \
         patch("core.task_service.vfs_manager.broadcast_plan_document", broadcast_plan), \
         patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.request_human_approval", request_human_approval):
        await ts._run_coding_task("sess-checklist", _payload(), "SEQUENTIAL")

    # Two broadcasts: the early seed (empty summary) then the turn-end one.
    assert broadcast_plan.await_count >= 2
    summaries: List[str] = [c.args[1].summary for c in broadcast_plan.await_args_list]
    assert summaries[0] == ""              # early seed carries no summary
    assert any(s for s in summaries[1:])   # a later broadcast carries the real summary
    # The seeded payload carries the WBS so the checklist can render at once.
    assert broadcast_plan.await_args_list[0].args[1].tasks[0]["step_number"] == 1
