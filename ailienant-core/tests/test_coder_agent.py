# tests/test_coder_agent.py
"""Phase 4.1.4 DoD — CoderAgent Cognitive Policy Engine + 8-role schema widening.

Four tests cover:
  A. Tool whitelist resolution (doc_manager — no BashTool).
  B. HITL flag emission when devops_infra touches .env.
  C. Ephemeral system prompt does NOT leak to state.messages OR appear as a
     non-state key in the result dict (R1 — LangGraph state-merge contract).
  D. Legacy 5-value target_role migrates to new 8-value canonical name
     end-to-end through the Coder.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_step(
    n: int = 1,
    role: str = "core_dev",
    action: str = "edit_file",
    target_file: str = "main.py",
    description: str = "Stub step.",
    status: str = "pending",
) -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role=role,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        target_file=target_file,
        description=description,
        status=status,  # type: ignore[arg-type]
    )


def _make_mission(tasks: List[WBSStep]) -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.",
        scope=["main.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=tasks,
        checks=["Pytest exits 0."],
    )


def _make_state(mission: MissionSpecification, step_id: int = 1, **overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "coder-test",
        "mission_spec": mission,
        "current_step_id": step_id,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _mock_websocket_emit() -> Any:
    """Suppress the real WebSocket broadcast in run_coder_node — tests run without uvicorn."""
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ):
        yield


# ── Test A — doc_manager tool whitelist ──────────────────────────────────────


def test_coder_agent_resolves_doc_manager_tool_whitelist() -> None:
    """doc_manager must NOT have BashTool; must have WriteFileTool + apply_patch."""
    from roles import ROLE_REGISTRY

    whitelist = ROLE_REGISTRY["doc_manager"]["allowed_tools"]
    assert "BashTool" not in whitelist
    assert "pytest" not in whitelist
    assert "WriteFileTool" in whitelist
    assert "apply_patch" in whitelist
    assert "FileReadTool" in whitelist


# ── Test B — devops_infra HITL flag on .env ──────────────────────────────────


@pytest.mark.anyio
async def test_coder_agent_emits_hitl_flag_when_devops_touches_dotenv() -> None:
    step = _make_step(
        role="devops_infra",
        action="write_file",
        target_file=".env",
        description="Update DATABASE_URL secret.",
    )
    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node

    result = await run_coder_node(state)

    assert "security_flags" in result
    flags: List[str] = result["security_flags"]
    matches = [f for f in flags if f.startswith("HITL_APPROVAL_REQUIRED:devops_infra:.env")]
    assert matches, f"Expected HITL flag for .env trigger, got: {flags}"


# ── Test C — ephemeral system prompt does NOT leak (R1: state-key contract) ──


@pytest.mark.anyio
async def test_coder_agent_ephemeral_system_prompt_does_not_leak_to_messages_or_state() -> None:
    step = _make_step(role="secops")
    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node
    from roles import build_coder_system_prompt

    result = await run_coder_node(state)

    # CRITICAL: the result dict must NOT contain any non-state key — LangGraph
    # would otherwise break state-merge or bloat the SQLite checkpoint.
    assert "messages" not in result
    assert "allowed_tools" not in result
    assert "ephemeral_system_prompt" not in result
    assert "role_config" not in result

    # Every returned key must be a declared field on AIlienantGraphState.
    allowed_state_keys = {
        "vfs_buffer",
        "target_role",
        "current_step_id",
        "current_cost_usd",
        "security_flags",
        "errors",
    }
    assert set(result.keys()) <= allowed_state_keys, (
        f"Coder returned non-state keys: {set(result.keys()) - allowed_state_keys}"
    )

    # The builder still produces the SecOps directive — proves the prompt is
    # constructable for Phase 5's MCP executor, just never persisted to state.
    secops_prompt = build_coder_system_prompt("secops")
    assert "OWASP Top-10 enforced" in secops_prompt
    assert "secops" in secops_prompt


# ── Test D — legacy role migrates end-to-end through the Coder ────────────────


@pytest.mark.anyio
async def test_coder_agent_legacy_role_migrates_to_new_via_validator() -> None:
    # Construct with legacy "Test" → before-validator maps to "qa_tester".
    step = _make_step(role="Test", target_file="tests/foo.py")
    assert step.target_role == "qa_tester", (
        "WBSStep before-validator must migrate legacy 'Test' to canonical "
        f"'qa_tester' on construction, got: {step.target_role}"
    )

    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node

    result = await run_coder_node(state)

    assert result["target_role"] == "qa_tester"
