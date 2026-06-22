"""Checkpoint gate for 8.10.13 — post-8.10.12 hardening.

- Researcher skeleton is hard-capped to the explicit ceiling (defense-in-depth
  beyond max_tokens); a normal skeleton passes through unchanged.
- The Planner clears `researcher_skeleton` from state after consuming it, so the
  buffer does not ride downstream coder / agentic-cell checkpoints.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.researcher import _SKELETON_MAX_CHARS
from brain.state import MissionSpecification, WBSStep


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _llm(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    return resp


def _researcher_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "rsrch-8-10-13",
        "user_input": "what is recursion?",
        "workspace_root": "/ws",
        "project_id": "p1",
        "explicit_mentions": [],
        "session_permission_mode": "AUTO",
        "errors": [],
    }
    state.update(overrides)
    return state


def _run_researcher(content: str) -> Dict[str, Any]:
    from agents.researcher import run_researcher_node

    # fast_track True → grounding loop + retrieval are skipped; the skeleton ainvoke
    # is the only LLM call, so its content is what the ceiling guard sees.
    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=True
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "agents.researcher.LLMGateway.ainvoke", return_value=_llm(content)
    ):
        return asyncio.run(run_researcher_node(_researcher_state(), None))


# ──────────────────────────────────────────────────────────────────────────────
# Skeleton ceiling
# ──────────────────────────────────────────────────────────────────────────────


def test_oversized_skeleton_is_truncated_to_ceiling() -> None:
    oversized = "x" * (_SKELETON_MAX_CHARS + 5000)
    result = _run_researcher(oversized)
    skeleton = result["researcher_skeleton"]
    assert skeleton.endswith("[skeleton truncated]")
    assert len(skeleton) <= _SKELETON_MAX_CHARS + len("\n…[skeleton truncated]")


def test_normal_skeleton_passes_through_unchanged() -> None:
    normal = "## Skeleton\n- core/auth.py: validate(token: str) -> bool"
    result = _run_researcher(normal)
    assert result["researcher_skeleton"] == normal


# ──────────────────────────────────────────────────────────────────────────────
# Skeleton lifecycle — Planner clears it after consumption
# ──────────────────────────────────────────────────────────────────────────────


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


def _broker_decision() -> MagicMock:
    d = MagicMock()
    d.cancelled = False
    d.effective_model = "ailienant/big"
    d.holds_lock = False
    return d


def _planner_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "planner-8-10-13",
        "user_input": "Add a feature.",
        "workspace_root": "/ws",
        "project_id": "p1",
        "context_metrics": None,
        "mission_spec": None,
        "immutable_wbs": None,
        "errors": [],
        "tci": 45.0,
        "css": 78.5,
        "provider": "LOCAL",
        "dirty_buffers": [],
        "ide_context": "",
        "researcher_skeleton": None,
    }
    state.update(overrides)
    return state


@pytest.mark.anyio
async def test_planner_clears_skeleton_after_consuming_it() -> None:
    skeleton = "## Skeleton\n- core/auth.py: handles JWT validation"
    mock_ainvoke = AsyncMock(return_value=_llm(_valid_mission_json()))
    mock_acquire = AsyncMock(return_value=_broker_decision())

    state = _planner_state(researcher_skeleton=skeleton)

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "agents.planner.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "agents.planner.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", AsyncMock()
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    # Consumed-then-cleared: the channel is explicitly reset to None on the delta …
    assert "researcher_skeleton" in result
    assert result["researcher_skeleton"] is None
    # … but consumption happened first — the skeleton text reached the prompt.
    sent: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    joined = "\n".join(m["content"] for m in sent)
    assert "core/auth.py: handles JWT validation" in joined
