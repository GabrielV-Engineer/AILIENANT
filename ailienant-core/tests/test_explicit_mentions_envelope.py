# tests/test_explicit_mentions_envelope.py
"""Phase 7.11.4 (ADR-706 §4.5d) — hard-context envelope contract.

The researcher's @-mention bypass path must wrap each forced block in the
literal ``[HARD CONTEXT: SOURCE FILE <path>]`` envelope so the LLM treats the
material as authoritative user-supplied content (no summarisation / RAG
filtering). These tests only assert the *envelope shape* — the bypass
behaviour itself is already covered by ``test_phase4_researcher.py``.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.context_auditor import RiskLevel


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "envelope-test",
        "user_input": "Audit the auth module.",
        "workspace_root": "/ws",
        "project_id": "abc123",
        "explicit_mentions": [],
        "errors": [],
    }
    state.update(overrides)
    return state


def _captured_system_prompt(mock_ainvoke: AsyncMock) -> str:
    """Extract the system-prompt string from the captured LLMGateway.ainvoke call."""
    assert mock_ainvoke.call_count == 1, (
        f"researcher should invoke the LLM exactly once; got {mock_ainvoke.call_count}"
    )
    kwargs = mock_ainvoke.call_args.kwargs
    messages: List[Dict[str, str]] = kwargs["messages"]
    assert messages[0]["role"] == "system", "first message must be the system prompt"
    return messages[0]["content"]


# ── Test 1 — envelope appears verbatim for each mention ──────────────────────


@pytest.mark.anyio
async def test_researcher_emits_hard_context_envelope_for_each_mention() -> None:
    """`[HARD CONTEXT: SOURCE FILE <path>]` must appear in the system prompt
    for every successfully-read mention. Multiple mentions → multiple envelopes."""
    mock_vfs_instance = MagicMock()
    mock_vfs_instance.read = MagicMock(side_effect=["alpha-body\n", "beta-body\n"])

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content="## Skeleton"))
    ]
    mock_ainvoke = AsyncMock(return_value=mock_llm_response)

    state = _base_state(explicit_mentions=["src/alpha.ts", "src/beta.ts"])

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.vfs_middleware.VFSMiddleware", return_value=mock_vfs_instance
    ), patch("agents.researcher.LLMGateway.ainvoke", mock_ainvoke):
        from agents.researcher import run_researcher_node

        await run_researcher_node(state)

    system_prompt = _captured_system_prompt(mock_ainvoke)
    # Both envelopes must be present, verbatim, in the captured system prompt.
    assert "[HARD CONTEXT: SOURCE FILE src/alpha.ts]" in system_prompt
    assert "[HARD CONTEXT: SOURCE FILE src/beta.ts]" in system_prompt
    # And the file bodies must follow each envelope (no body lost).
    assert "alpha-body" in system_prompt
    assert "beta-body" in system_prompt


# ── Test 2 — missing paths are skipped, fail-soft contract preserved ─────────


@pytest.mark.anyio
async def test_researcher_skips_missing_mentions_without_raising() -> None:
    """`FileNotFoundError` from VFS must be swallowed; the missing path must
    NOT appear in the envelope; the LLM call still fires with the remaining
    successful blocks."""
    mock_vfs_instance = MagicMock()

    def _read(path: str) -> str:
        if path == "src/ghost.ts":
            raise FileNotFoundError(path)
        return "real-content\n"

    mock_vfs_instance.read = MagicMock(side_effect=_read)

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content="## Skeleton"))
    ]
    mock_ainvoke = AsyncMock(return_value=mock_llm_response)

    state = _base_state(explicit_mentions=["src/ghost.ts", "src/real.ts"])

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.vfs_middleware.VFSMiddleware", return_value=mock_vfs_instance
    ), patch("agents.researcher.LLMGateway.ainvoke", mock_ainvoke):
        from agents.researcher import run_researcher_node

        result = await run_researcher_node(state)

    # Researcher returned a skeleton — did not raise on the missing path.
    assert isinstance(result.get("researcher_skeleton"), str)

    system_prompt = _captured_system_prompt(mock_ainvoke)
    assert "[HARD CONTEXT: SOURCE FILE src/real.ts]" in system_prompt
    # The missing path must NOT have produced an envelope.
    assert "src/ghost.ts" not in system_prompt
