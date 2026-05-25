# tests/test_planner.py
"""Phase 4.1.2 DoD — PlannerAgent gap closure tests.

Covers the three new behaviours:
  1. Bounded ValidationError retry (1 retry → success).
  2. Retry exhaustion → clean error return (no fatal crash).
  3. researcher_skeleton consumption (Phase 4.1.1 channel wired into the prompt).

All tests use the established anyio + AsyncMock + patch pattern from
tests/test_fast_boot.py:191-262.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _valid_mission_json() -> str:
    """Minimal MissionSpecification that satisfies the strict Pydantic schema."""
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
    """Shape an LLMGateway.ainvoke response mock around a string body."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


def _broker_decision() -> MagicMock:
    """Stub a ResourceBroker decision: lock not held, not cancelled, BIG tier."""
    decision = MagicMock()
    decision.cancelled = False
    decision.effective_model = "ailienant/big"
    decision.holds_lock = False
    return decision


def _base_state(**overrides: Any) -> Dict[str, Any]:
    """Minimal AIlienantGraphState slice that satisfies run_planner_node."""
    state: Dict[str, Any] = {
        "task_id": "planner-test",
        "user_input": "Add a feature.",
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
    state.update(overrides)
    return state


# ── Test 1: retry-then-succeed ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_retries_on_malformed_json_then_succeeds() -> None:
    """First LLM call returns garbage; second returns valid JSON.
    Planner must consume exactly 1 retry and succeed."""

    bad_response = _make_response("{ this is not valid json ")
    good_response = _make_response(_valid_mission_json())

    mock_ainvoke = AsyncMock(side_effect=[bad_response, good_response])
    mock_search = AsyncMock(return_value=(0.8, ["test/scope.py"]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=0.6,
            context_block="",
            parsed_files=["test/scope.py"],
            target_files=["test/scope.py"],
        )
    )
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, patch(
        "agents.planner.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "agents.planner.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "agents.planner.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=True
    ), patch(
        "agents.planner.audit_task_complexity",
        AsyncMock(return_value=__import__("core.memory.context_auditor",
                                          fromlist=["RiskLevel"]).RiskLevel.NONE),
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    assert result.get("planner_retry_count") == 1
    assert mock_ainvoke.call_count == 2

    # The second call's user message must contain the corrective banner.
    # Phase 7.10.4 (ADR-704) — corrective now names the envelope failure mode + feeds errors.
    second_call_messages: List[Dict[str, str]] = mock_ainvoke.call_args_list[1].kwargs[
        "messages"
    ]
    corrective = second_call_messages[-1]["content"]
    assert "failed schema validation with these errors" in corrective
    assert "DO NOT wrap it in any top-level key" in corrective


# ── Test 2: retries exhausted → clean error return ────────────────────────────


@pytest.mark.anyio
async def test_planner_returns_errors_when_retries_exhausted() -> None:
    """All three attempts return garbage. Planner must surface a clean error,
    never raise, and never produce a mission_spec."""

    garbage = _make_response("definitely not json")
    mock_ainvoke = AsyncMock(side_effect=[garbage, garbage, garbage])
    mock_search = AsyncMock(return_value=(0.8, []))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=0.0, context_block="", parsed_files=[], target_files=[]
        )
    )
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, patch(
        "agents.planner.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "agents.planner.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "agents.planner.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=True
    ), patch(
        "agents.planner.audit_task_complexity",
        AsyncMock(return_value=__import__("core.memory.context_auditor",
                                          fromlist=["RiskLevel"]).RiskLevel.NONE),
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is None
    assert "errors" in result and result["errors"]
    assert "schema validation exhausted" in result["errors"][0]
    assert mock_ainvoke.call_count == 3
    assert result.get("planner_retry_count") == 3


# ── Test 3: researcher_skeleton consumption ───────────────────────────────────


@pytest.mark.anyio
async def test_planner_consumes_researcher_skeleton() -> None:
    """When researcher_skeleton is present in state, its content must appear
    inside the prompt sent to LLMGateway.ainvoke."""

    skeleton = "## Skeleton\n- core/auth.py: handles JWT validation"
    good_response = _make_response(_valid_mission_json())

    mock_ainvoke = AsyncMock(return_value=good_response)
    mock_search = AsyncMock(return_value=(0.8, []))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=0.0, context_block="", parsed_files=[], target_files=[]
        )
    )
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state(researcher_skeleton=skeleton)

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, patch(
        "agents.planner.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "agents.planner.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "agents.planner.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=True
    ), patch(
        "agents.planner.audit_task_complexity",
        AsyncMock(return_value=__import__("core.memory.context_auditor",
                                          fromlist=["RiskLevel"]).RiskLevel.NONE),
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    mock_ainvoke.assert_called_once()

    # The skeleton text must surface in either the system prompt or user message.
    sent_messages: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    joined = "\n".join(m["content"] for m in sent_messages)
    assert "core/auth.py: handles JWT validation" in joined
