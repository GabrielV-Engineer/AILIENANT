# tests/test_phase4_researcher.py
"""ResearcherAgent standard retrieval + @-mention override.

The Researcher is a first-class node that owns retrieval + the routing cascade and
runs a bounded READ_ONLY grounding loop before composing the skeleton. These tests
inject a no-op grounding reasoner and stub the mini-judge so the only live LLM call
is the skeleton compression, and they seal the fast-boot snapshot so retrieval is
deterministic.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.recency import session_heatmap
from brain.state import ContextMeter
from core.memory.context_auditor import RiskLevel


@pytest.fixture(autouse=True)
def _reset_heatmap() -> Any:
    """Keep the process-singleton recency heatmap isolated between tests."""
    session_heatmap.reset()
    yield
    session_heatmap.reset()


async def _noop_reasoner(_messages: Sequence[Dict[str, Any]]) -> str:
    return "{}"


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
    """Without explicit_mentions, the node hits SemanticMemoryManager and
    GraphRAGDynamicExtractor, surfaces the LLM's skeleton, and emits the routing
    signal. The grounding loop is a no-op (injected reasoner) so the only live LLM
    call is the skeleton."""
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
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ) as mock_ainvoke:
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    assert result["researcher_skeleton"].startswith("## Skeleton")
    assert isinstance(result["context_metrics"], ContextMeter)
    assert result["provider"] in {"LOCAL", "CLOUD"}
    mock_search.assert_called_once()
    mock_deep_parse.assert_called_once()
    mock_ainvoke.assert_called_once()  # skeleton only — grounding loop is a no-op


# ── Test 2 — @-mention override bypasses GraphRAG ────────────────────────────


@pytest.mark.anyio
async def test_researcher_explicit_override() -> None:
    """With explicit_mentions present, VFSMiddleware.read is used directly, the
    grounding loop + GraphRAG/Semantic search are bypassed, and the node still emits
    a routing signal."""

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

    state = _base_state(explicit_mentions=["app/auth.py", "app/models.py"])

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor",
        side_effect=_graphrag_should_not_run,
    ), patch(
        "core.vfs_middleware.VFSMiddleware", return_value=mock_vfs_instance
    ) as mock_vfs_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ) as mock_ainvoke:
        mock_sem_cls.return_value.search_with_paths = MagicMock(
            side_effect=AssertionError("search must not run on the @-mention path")
        )

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(state)

    assert result["researcher_skeleton"].startswith("## Skeleton")
    assert isinstance(result["context_metrics"], ContextMeter)
    assert mock_vfs_cls.called
    read_calls = [c.args[0] for c in mock_vfs_instance.read.call_args_list]
    assert "app/auth.py" in read_calls
    assert "app/models.py" in read_calls
    mock_ainvoke.assert_called_once()


# ── Shared scaffolding for the harness-improvement hook tests below ──────────


def _mock_retrieval() -> tuple[AsyncMock, AsyncMock, MagicMock]:
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
    mock_llm_response.choices = [MagicMock(message=MagicMock(content="## Skeleton"))]
    return mock_search, mock_deep_parse, mock_llm_response


# ── Test 3 — ambiguity pre-flight gate enriches the prompt via clarification ─


@pytest.mark.anyio
async def test_researcher_ambiguity_gate_enriches_prompt_via_clarification() -> None:
    """An underspecified prompt pauses for clarification before retrieval, and the
    answer is folded into the user_input the skeleton LLM call actually sees."""
    mock_search, mock_deep_parse, mock_llm_response = _mock_retrieval()
    state = _base_state(user_input="fix this")

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.is_underspecified", return_value=True
    ), patch(
        "core.hitl.request_graph_clarification",
        return_value={"answer": None, "selected_option": "Use the active editor file"},
    ) as mock_clarify, patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ) as mock_ainvoke:
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    mock_clarify.assert_called_once()
    assert mock_clarify.call_args.kwargs["session_id"] == "researcher-test"
    skeleton_prompt = mock_ainvoke.call_args.kwargs["messages"][1]["content"]
    assert "Use the active editor file" in skeleton_prompt
    assert result["researcher_skeleton"].startswith("## Skeleton")


# ── Test 4 — HIGH risk offers a Plan-mode switch ─────────────────────────────


@pytest.mark.anyio
async def test_researcher_high_risk_suggests_plan_mode() -> None:
    """A HIGH Mini-Judge verdict offers a Plan-mode switch; accepting it surfaces
    session_permission_mode=PLAN_ONLY in the node's result."""
    mock_search, mock_deep_parse, mock_llm_response = _mock_retrieval()
    state = _base_state(
        user_input=(
            "Refactor the entire multi-tenant billing subsystem across all "
            "12 service modules."
        ),
        session_permission_mode="STANDARD",
    )

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.is_underspecified", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.HIGH)
    ), patch(
        "core.hitl.request_graph_clarification",
        return_value={"answer": None, "selected_option": "Switch to Plan mode"},
    ) as mock_clarify, patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    mock_clarify.assert_called_once()
    assert result.get("session_permission_mode") == "PLAN_ONLY"
    assert result["provider"] == "CLOUD"  # HIGH risk still vetoes routing to CLOUD


@pytest.mark.anyio
async def test_researcher_high_risk_skips_suggestion_when_already_plan_mode() -> None:
    """Already in Plan mode → no repeated clarification prompt every turn."""
    mock_search, mock_deep_parse, mock_llm_response = _mock_retrieval()
    state = _base_state(
        user_input=(
            "Refactor the entire multi-tenant billing subsystem across all "
            "12 service modules."
        ),
        session_permission_mode="PLAN_ONLY",
    )

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.is_underspecified", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.HIGH)
    ), patch(
        "core.hitl.request_graph_clarification"
    ) as mock_clarify, patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    mock_clarify.assert_not_called()
    assert "session_permission_mode" not in result


# ── Test 5 — preclassified_risk skips the duplicate Mini-Judge call ──────────


@pytest.mark.anyio
async def test_researcher_reuses_preclassified_risk_skips_mini_judge() -> None:
    """A same-turn preclassified_risk hint (from TaskService._classify_intent_and_risk)
    skips the duplicate audit_task_complexity LLM round-trip."""
    mock_search, mock_deep_parse, mock_llm_response = _mock_retrieval()
    state = _base_state(
        user_input=(
            "Refactor the entire multi-tenant billing subsystem across all "
            "12 service modules."
        ),
        preclassified_risk="HIGH",
    )
    mock_audit = AsyncMock(return_value=RiskLevel.NONE)

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.is_underspecified", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=mock_audit
    ), patch(
        "core.hitl.request_graph_clarification",
        return_value={"answer": None, "selected_option": "Keep current mode"},
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", return_value=mock_llm_response
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    mock_audit.assert_not_called()  # preclassified_risk short-circuited the Mini-Judge call
    assert result["provider"] == "CLOUD"  # HIGH still vetoes routing, just from the hint
