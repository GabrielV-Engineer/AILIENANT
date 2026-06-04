# tests/test_fast_boot.py
"""Phase 3.6 DoD — Cognitive Fast-Boot: AGENTS.md dump/load/merge + planner shortcut."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.recency import session_heatmap
from brain.state import ContextMeter, MissionSpecification, WBSStep
from core.state_manager import (
    CachedAgentState,
    _agents_md_path,
    _JSON_START,
    _parse_cached_state,
    dump_state_to_markdown,
    load_state_from_markdown,
    record_merge_event,
)


@pytest.fixture(autouse=True)
def _reset_heatmap() -> Any:
    """Keep the process-singleton recency heatmap isolated between tests."""
    session_heatmap.reset()
    yield
    session_heatmap.reset()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_mission(outcome: str = "test outcome") -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome,
        scope=["core/"],
        constraints=["no new deps"],
        decisions=["use aiosqlite"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="architect_refactor",
                action="write_file",
                target_file="core/janitor.py",
                description="implement janitor",
            )
        ],
        checks=["pytest passes"],
    )


def _make_context() -> ContextMeter:
    return ContextMeter(
        semantic_similarity=0.82,
        graph_coverage=0.65,
        recency_score=0.9,
        css_total=78.5,
        task_complexity_index=45.0,
        routing_decision="LOCAL_BIG",
        is_red_alert=False,
    )


def _make_state(workspace_root: str, mission: MissionSpecification, ctx: ContextMeter) -> dict[str, Any]:
    return {
        "mission_spec": mission,
        "context_metrics": ctx,
        "task_id": "task-abc",
        "workspace_root": workspace_root,
        "_top_k_files_cache": ["core/janitor.py", "brain/state.py"],
    }


# ── Test 1: dump writes AGENTS.md with correct content ────────────────────────

def test_dump_state_writes_agents_md(tmp_path: Path) -> None:
    ws = str(tmp_path)
    mission = _make_mission()
    ctx = _make_context()
    state = _make_state(ws, mission, ctx)

    result = dump_state_to_markdown(state, ws)

    assert result is True
    agents_md = _agents_md_path(ws)
    assert agents_md.exists()
    content = agents_md.read_text(encoding="utf-8")
    assert "AILIENANT Fast-Boot Checkpoint" in content
    assert "test outcome" in content
    assert _JSON_START.strip() in content

    # Machine JSON must round-trip correctly
    parsed = _parse_cached_state(content)
    assert parsed is not None
    assert parsed.task_id == "task-abc"
    assert parsed.mission_spec is not None
    assert parsed.mission_spec.outcome == "test outcome"
    assert parsed.context_metrics is not None
    assert abs(parsed.context_metrics.css_total - 78.5) < 0.01
    assert "core/janitor.py" in parsed.top_k_files


# ── Test 2: dump uses atomic write (os.replace is called) ─────────────────────

def test_dump_state_atomic_write_pattern(tmp_path: Path, monkeypatch: Any) -> None:
    replace_calls: list[tuple[str, str]] = []
    original_replace = os.replace

    def _spy_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        original_replace(src, dst)

    monkeypatch.setattr("core.state_manager.os.replace", _spy_replace)

    ws = str(tmp_path)
    state = _make_state(ws, _make_mission(), _make_context())
    dump_state_to_markdown(state, ws)

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert ".__ailienant_tmp_" in src
    assert dst.endswith("AGENTS.md")
    # src and dst must be in the same directory (atomic rename constraint)
    assert os.path.dirname(src) == os.path.dirname(dst)


# ── Test 3: load returns None when AGENTS.md is missing ───────────────────────

def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    result = load_state_from_markdown(str(tmp_path))
    assert result is None


# ── Test 4: load returns None when AGENTS.md is stale ─────────────────────────

def test_load_returns_none_when_stale(tmp_path: Path) -> None:
    ws = str(tmp_path)
    state = _make_state(ws, _make_mission(), _make_context())
    dump_state_to_markdown(state, ws)

    agents_md = _agents_md_path(ws)
    # Back-date the mtime by 2 hours
    two_hours_ago = time.time() - 7200
    os.utime(str(agents_md), (two_hours_ago, two_hours_ago))

    result = load_state_from_markdown(ws, max_age_seconds=3600)
    assert result is None


# ── Test 5: load returns CachedAgentState when AGENTS.md is fresh ─────────────

def test_load_returns_cached_state_when_fresh(tmp_path: Path) -> None:
    ws = str(tmp_path)
    mission = _make_mission("fresh mission")
    ctx = _make_context()
    state = _make_state(ws, mission, ctx)
    dump_state_to_markdown(state, ws)

    result = load_state_from_markdown(ws, max_age_seconds=3600)

    assert result is not None
    assert isinstance(result, CachedAgentState)
    assert result.mission_spec is not None
    assert result.mission_spec.outcome == "fresh mission"
    assert result.context_metrics is not None
    assert result.context_metrics.routing_decision == "LOCAL_BIG"
    assert result.top_k_files == ["core/janitor.py", "brain/state.py"]


# ── Test 6: record_merge_event updates AGENTS.md with merge metadata ──────────

def test_record_merge_event_updates_agents_md(tmp_path: Path) -> None:
    ws = str(tmp_path)
    state = _make_state(ws, _make_mission(), _make_context())
    dump_state_to_markdown(state, ws)

    merged = ["core/janitor.py", "brain/daemon.py"]
    result = record_merge_event(ws, merged)

    assert result is True
    loaded = load_state_from_markdown(ws)
    assert loaded is not None
    assert loaded.last_merge_at is not None
    assert "core/janitor.py" in loaded.last_merged_paths
    assert "brain/daemon.py" in loaded.last_merged_paths
    # Original mission spec must be preserved
    assert loaded.mission_spec is not None


# ── Test 7: record_merge_event is no-op when AGENTS.md doesn't exist ──────────

def test_record_merge_event_noop_when_missing(tmp_path: Path) -> None:
    result = record_merge_event(str(tmp_path), ["some/file.py"])
    assert result is False


# ── Test 8 (anyio): planner skips LanceDB when AGENTS.md is fresh ─────────────

@pytest.mark.anyio
async def test_planner_skips_lancedb_when_cache_fresh() -> None:
    """Prove that run_planner_node calls load_state_from_markdown and, when it
    returns a valid CachedAgentState, SemanticMemoryManager.search_with_paths is
    NOT called."""
    cached = CachedAgentState(
        mission_spec=_make_mission("cached plan"),
        context_metrics=_make_context(),
        top_k_files=["brain/state.py"],
        task_id="t1",
        generated_at="2026-05-16T00:00:00+00:00",
    )

    # Minimal LangGraph state that satisfies planner pre-conditions
    state: dict[str, Any] = {
        "task_id": "t1",
        "user_input": "add a feature",
        "workspace_root": "/ws",
        "project_id": "abc123",
        "context_metrics": _make_context(),
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
    }

    mock_search = AsyncMock(return_value=(0.8, ["brain/state.py"], [""]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=0.6,
            context_block="",
            parsed_files=["brain/state.py"],
            target_files=["brain/state.py"],
        )
    )
    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content=_make_mission("llm plan").model_dump_json()))
    ]

    with patch("agents.planner.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=cached), \
         patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, \
         patch("agents.planner.GraphRAGDynamicExtractor") as mock_extractor_cls, \
         patch("agents.planner.LLMGateway.ainvoke", return_value=mock_llm_response), \
         patch("agents.planner.TrajectoryMemoryManager") as mock_traj_cls, \
         patch("core.state_manager.dump_state_to_markdown", return_value=True):

        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node
        await run_planner_node(state)

    # LanceDB search must NOT have been called — fast-boot served the context
    mock_search.assert_not_called()
    # The deep_parse (cheap I/O) must still run to pick up file changes
    mock_deep_parse.assert_called_once()
