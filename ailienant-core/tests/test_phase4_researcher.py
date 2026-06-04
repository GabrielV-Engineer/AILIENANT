# tests/test_phase4_researcher.py
"""Phase 4.1.1 DoD — ResearcherAgent standard retrieval + @-mention override."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.recency import session_heatmap


@pytest.fixture(autouse=True)
def _reset_heatmap() -> Any:
    """Keep the process-singleton recency heatmap isolated between tests."""
    session_heatmap.reset()
    yield
    session_heatmap.reset()


def _base_state(**overrides: Any) -> Dict[str, Any]:
    """Minimal AIlienantGraphState slice that satisfies run_researcher_node."""
    state: Dict[str, Any] = {
        "task_id": "researcher-test",
        "user_input": "Refactor the auth module for clarity.",
        "workspace_root": "/ws",
        "project_id": "abc123",
        "explicit_mentions": [],
        "errors": [],
    }
    state.update(overrides)
    return state


# ── Test 1 — standard retrieval path (GraphRAG, no @-mentions) ───────────────


@pytest.mark.anyio
async def test_researcher_standard_retrieval() -> None:
    """Without explicit_mentions, the node must hit SemanticMemoryManager
    and GraphRAGDynamicExtractor and surface the LLM's skeleton output."""
    mock_search = AsyncMock(return_value=(0.7, ["core/state.py"], [""]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            context_block="STUB_BLOCK",
            parsed_files=["core/state.py"],
            target_files=["core/state.py"],
            coverage_ratio=1.0,
        )
    )
    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(
            message=MagicMock(
                content="## Skeleton\n- core/state.py: AIlienantGraphState definition"
            )
        )
    ]

    state = _base_state()

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "agents.researcher.LLMGateway.ainvoke", return_value=mock_llm_response
    ) as mock_ainvoke, patch(
        "core.vfs_middleware.VFSMiddleware"
    ) as mock_vfs_cls:
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(state)

    assert "researcher_skeleton" in result
    assert isinstance(result["researcher_skeleton"], str)
    assert result["researcher_skeleton"].startswith("## Skeleton")
    mock_search.assert_called_once()
    mock_deep_parse.assert_called_once()
    mock_ainvoke.assert_called_once()
    # VFSMiddleware must NOT be instantiated when there are no @-mentions.
    mock_vfs_cls.assert_not_called()


# ── Test 2 — @-mention override bypasses GraphRAG ────────────────────────────


@pytest.mark.anyio
async def test_researcher_explicit_override() -> None:
    """With explicit_mentions present, VFSMiddleware.read is used directly
    and the GraphRAG/Semantic search path must NOT fire."""

    def _semantic_should_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "SemanticMemoryManager.search_with_paths must not be called "
            "when explicit_mentions is non-empty."
        )

    def _graphrag_should_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "GraphRAGDynamicExtractor must not be instantiated when "
            "explicit_mentions is non-empty."
        )

    mock_vfs_instance = MagicMock()
    mock_vfs_instance.read = MagicMock(return_value="# fake file content\n")

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content="## Skeleton (forced)"))
    ]

    state = _base_state(
        explicit_mentions=["app/auth.py", "app/models.py"],
    )

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor",
        side_effect=_graphrag_should_not_run,
    ), patch(
        "core.vfs_middleware.VFSMiddleware", return_value=mock_vfs_instance
    ) as mock_vfs_cls, patch(
        "agents.researcher.LLMGateway.ainvoke", return_value=mock_llm_response
    ) as mock_ainvoke:
        mock_sem_cls.return_value.search_with_paths = MagicMock(
            side_effect=_semantic_should_not_run
        )

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(state)

    assert isinstance(result.get("researcher_skeleton"), str)
    assert result["researcher_skeleton"].startswith("## Skeleton")
    # VFS reads happened, GraphRAG did not.
    assert mock_vfs_cls.called
    read_calls = [c.args[0] for c in mock_vfs_instance.read.call_args_list]
    assert "app/auth.py" in read_calls
    assert "app/models.py" in read_calls
    mock_ainvoke.assert_called_once()
