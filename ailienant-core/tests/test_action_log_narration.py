"""Observability — live action-log + failure-pivot narration (ADR-731).

The agent's reads and self-heal pivots flow through the existing
``state["narrate"]`` seam (a metered ``server_pipeline_step`` emitter injected by
the task service), so the IDE action-log shows *what* the agent is doing without
a second HUD or a new wire contract. These tests pin the two narration contracts
by calling the graph nodes directly with a list-capturing ``narrate`` stub:

  A. ``run_coder_node`` announces the file it is about to read (basename only).
  B. ``run_error_correction_node`` narrates a self-heal pivot in plain language —
     a transient fault (a model timeout) reads as a deliberate retry, with an
     outcome note (recovered / could-not-fix) on either branch.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from brain.failure_breaker import normalize_signature
from brain.state import MissionSpecification, WBSStep


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _capture() -> tuple[List[str], Any]:
    """Return ``(captured, narrate)`` — a list and an async stub appending to it."""
    captured: List[str] = []

    async def _narrate(node_name: str, step_id: Optional[int] = None) -> None:
        captured.append(node_name)

    return captured, _narrate


# ── A — coder narrates the file it reads ──────────────────────────────────────


def _fake_llm_response(content: str) -> Any:
    """Minimal stand-in for a litellm ModelResponse (resp.choices[0].message.content)."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _coder_state(narrate: Any, *, target_file: str) -> Dict[str, Any]:
    mission = MissionSpecification(
        outcome="Test outcome.",
        scope=[target_file],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="core_dev",
                action="edit_file",
                target_file=target_file,
                description="Stub step.",
                status="pending",
            )
        ],
        checks=["Pytest exits 0."],
    )
    return {
        "task_id": "narration-test",
        "project_id": "",
        "workspace_root": "",
        "mission_spec": mission,
        "current_step_id": 1,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
        "narrate": narrate,
    }


@pytest.mark.anyio
async def test_coder_narrates_basename_of_read_target() -> None:
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    captured, narrate = _capture()
    state = _coder_state(narrate, target_file="src/app.py")

    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content="def foo():\n    return 1\n"),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response('{"edits": []}')),
    ):
        await run_coder_node(state)

    # Basename, not full path — the workspace structure stays private and the
    # narration-gate charge small.
    assert "reading app.py" in captured
    assert not any("src/app.py" in c for c in captured)


# ── B — error-correction narrates a plain-language pivot ──────────────────────


def _heal_state(narrate: Any, signature: str, *, step_id: Optional[int] = 3) -> Dict[str, Any]:
    return {
        "healing_required": True,
        "failed_node": "run_coder_node",
        "failure_signature": signature,
        "last_error_trace": 'File "src/app.py", line 1\nTimeout: request timed out',
        "current_step_id": step_id,
        "workspace_root": "",
        "mission_spec": None,
        "narrate": narrate,
    }


@pytest.mark.anyio
async def test_pivot_narrates_timeout_in_plain_language_and_concedes() -> None:
    from agents.error_correction import CorrectionResult, run_error_correction_node

    captured, narrate = _capture()
    signature = normalize_signature("run_coder_node", "Timeout", "request timed out")
    state = _heal_state(narrate, signature, step_id=3)

    with patch(
        "agents.error_correction._default_agent.propose_fix",
        new=AsyncMock(return_value=CorrectionResult(healed=False, diagnosis="no safe fix")),
    ):
        await run_error_correction_node(state)

    pivot = next((c for c in captured if c.startswith("self-healing")), "")
    assert "run_coder_node" in pivot
    assert "the model timed out" in pivot
    assert "retrying step 3" in pivot
    assert "could not auto-fix run_coder_node" in captured


@pytest.mark.anyio
async def test_pivot_narrates_connection_drop_and_recovery() -> None:
    from agents.error_correction import CorrectionResult, run_error_correction_node

    captured, narrate = _capture()
    signature = normalize_signature("run_coder_node", "APIConnectionError", "connection reset")
    state = _heal_state(narrate, signature, step_id=2)

    with patch(
        "agents.error_correction._default_agent.propose_fix",
        new=AsyncMock(
            return_value=CorrectionResult(
                healed=True,
                diagnosis="patched the off-by-one",
                pending_patches={"src/app.py": "@@ diff @@"},
                pending_contents={"src/app.py": "fixed"},
                pending_base_hash={"src/app.py": "abc"},
            )
        ),
    ):
        await run_error_correction_node(state)

    pivot = next((c for c in captured if c.startswith("self-healing")), "")
    assert "the connection dropped" in pivot
    assert "recovered run_coder_node" in captured


@pytest.mark.anyio
async def test_pivot_omits_retry_clause_when_step_unknown() -> None:
    from agents.error_correction import CorrectionResult, run_error_correction_node

    captured, narrate = _capture()
    signature = normalize_signature("run_coder_node", "Timeout", "request timed out")
    state = _heal_state(narrate, signature, step_id=None)

    with patch(
        "agents.error_correction._default_agent.propose_fix",
        new=AsyncMock(return_value=CorrectionResult(healed=False, diagnosis="no fix")),
    ):
        await run_error_correction_node(state)

    pivot = next((c for c in captured if c.startswith("self-healing")), "")
    assert "the model timed out" in pivot
    assert "retrying step" not in pivot
