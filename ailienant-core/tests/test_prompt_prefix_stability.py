# tests/test_prompt_prefix_stability.py
"""Manifest 12.1 — prompt-prefix stability (prerequisite for provider caching).

The per-turn `uuid.uuid4().hex` sandbox nonce used to be interpolated directly
into the planner's and coder's SYSTEM message, which made the prefix change on
every single call — defeating both provider prompt caching and a local
engine's own KV-prefix reuse. This suite locks in the fix: the system
message's leading bytes (identity, axiom, rules, project instructions) are now
byte-identical across repeated calls, and the per-turn nonce is declared in a
small trailing block instead.

An earlier draft of this fix relocated the nonce *declaration* into the user
turn. That was rejected in review: it puts declaration syntax in the same
message ROLE as untrusted content (a file, a RAG snippet), so injected text
could emit a competing declaration with no structural way for the model to
prefer the real one. SEAL2 below is the direct regression lock on that vector
— the declaration must live exclusively in the system role.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.prompts import build_boundary_declaration, build_static_identity_prompt
from brain.state import MissionSpecification, WBSStep
from core.response_cache import response_cache
from shared.rbac import PLANNER_IDENTITY

# The declaration marker used to split a system message into its byte-stable
# HEAD (everything before the marker — the cacheable prefix) and its per-turn
# TAIL (the marker onward). Kept as a literal here, independent of
# agents/prompts.py's exact wording, so the test fails loudly if the marker
# text ever drifts without an accompanying review of this suite.
_DELIMITER_MARKER = "=== 🔑 SECURE DELIMITER FOR THIS TURN ==="
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")

# Axiom/declaration vocabulary that must appear ONLY in the system message.
# Presence of any of these phrases in a user-turn message is exactly the
# injection vector review flagged: a declaration-style sentence living in the
# same role untrusted content can reach.
_AXIOM_PHRASES = (
    "SECURE DELIMITER FOR THIS TURN",
    "STRICTLY INERT DATA",
    "ignore any directive",
    "COGNITIVE QUARANTINE",
)


def _head(system_content: str) -> str:
    """The byte-stable portion of a system message, before the nonce marker."""
    return system_content.split(_DELIMITER_MARKER, 1)[0]


# =============================================================================
# PREFIX1 — build_static_identity_prompt is byte-identical and nonce-free
# =============================================================================


def test_static_identity_prompt_is_byte_identical_across_calls() -> None:
    first = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    second = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    assert first == second


def test_static_identity_prompt_never_contains_a_boundary_hex() -> None:
    """PREFIX3 — regression lock on the original defect: no per-turn nonce
    anywhere in the byte-stable identity block."""
    prompt = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    assert not _HEX32_RE.search(prompt), (
        f"static identity prompt must never embed a 32-hex-char nonce, found in: {prompt!r}"
    )


# =============================================================================
# SEAL1 — the boundary declaration is genuinely per-turn
# =============================================================================


def test_boundary_declaration_is_fresh_per_turn() -> None:
    decl_a = build_boundary_declaration("aaaa1111aaaa1111aaaa1111aaaa1111")
    decl_b = build_boundary_declaration("bbbb2222bbbb2222bbbb2222bbbb2222")
    assert decl_a != decl_b
    assert "aaaa1111aaaa1111aaaa1111aaaa1111" in decl_a
    assert "bbbb2222bbbb2222bbbb2222bbbb2222" in decl_b
    assert "aaaa1111aaaa1111aaaa1111aaaa1111" not in decl_b


# =============================================================================
# Planner integration — PREFIX2, SEAL2, DEGRADE1
# =============================================================================


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


def _planner_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


def _broker_decision() -> MagicMock:
    decision = MagicMock()
    decision.cancelled = False
    decision.effective_model = "ailienant/big"
    decision.holds_lock = False
    return decision


def _planner_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "prefix-stability-test",
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
        "ide_context": {},
        "researcher_skeleton": None,
    }
    state.update(overrides)
    return state


async def _run_planner_and_capture(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """Run run_planner_node with a hermetic LLM/broker stub; return the exact
    messages payload sent to LLMGateway.ainvoke."""
    mock_ainvoke = AsyncMock(return_value=_planner_response(_valid_mission_json()))
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

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

        await run_planner_node(state)

    mock_ainvoke.assert_called_once()
    messages: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    return messages


@pytest.mark.anyio
async def test_planner_system_head_is_byte_identical_across_two_turns() -> None:
    """PREFIX2 (planner) — the cacheable head must not depend on the per-turn
    nonce, even though the two turns get different boundaries."""
    response_cache.clear()
    messages_a = await _run_planner_and_capture(_planner_state())
    response_cache.clear()  # a second identical-key turn must not short-circuit to cache
    messages_b = await _run_planner_and_capture(_planner_state())

    system_a = messages_a[0]["content"]
    system_b = messages_b[0]["content"]
    assert _DELIMITER_MARKER in system_a
    assert _DELIMITER_MARKER in system_b
    assert _head(system_a) == _head(system_b)
    # The two turns really did get distinct nonces — this isn't vacuously true.
    assert system_a != system_b


@pytest.mark.anyio
async def test_planner_user_turn_carries_no_axiom_declaration_language() -> None:
    """SEAL2 (planner) — direct lock on the injection vector review raised: the
    delimiter's semantics must be declared only in the system role."""
    response_cache.clear()
    messages = await _run_planner_and_capture(_planner_state())
    user_turn = messages[1]["content"]
    for phrase in _AXIOM_PHRASES:
        assert phrase not in user_turn, (
            f"axiom/declaration phrase {phrase!r} leaked into the user turn: {user_turn!r}"
        )


@pytest.mark.anyio
async def test_planner_degrade_path_still_declares_the_boundary() -> None:
    """DEGRADE1 (planner) — under ContextBudgetError, the sandbox seal must
    survive even though rules/project-instructions/skills are (legitimately)
    shed. Losing the seal while the user turn still carries wrapped untrusted
    content would be a security regression, not a quality one."""
    response_cache.clear()
    with patch("agents.planner.resolve_context_budget", return_value=1):
        messages = await _run_planner_and_capture(_planner_state())

    system_content = messages[0]["content"]
    assert _DELIMITER_MARKER in system_content, (
        "boundary declaration must survive the identity-only degrade path"
    )


# =============================================================================
# Coder integration — PREFIX2, SEAL2, DEGRADE1
# =============================================================================


def _coder_llm_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _coder_step(target_file: str, description: str) -> WBSStep:
    return WBSStep(
        step_number=1,
        target_role="core_dev",
        action="edit_file",
        target_file=target_file,
        description=description,
        status="pending",
    )


def _coder_state(step: WBSStep, **overrides: Any) -> Dict[str, Any]:
    mission = MissionSpecification(
        outcome="Test outcome.",
        scope=[step.target_file],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[step],
        checks=["Pytest exits 0."],
    )
    state: Dict[str, Any] = {
        "task_id": "prefix-stability-coder-test",
        "mission_spec": mission,
        "current_step_id": step.step_number,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }
    state.update(overrides)
    return state


async def _run_coder_and_capture(
    state: Dict[str, Any], file_content: str = "def foo():\n    return 1\n"
) -> List[Dict[str, str]]:
    from core.vfs_middleware import VFSReadResult

    mock_ainvoke = AsyncMock(return_value=_coder_llm_response(""))
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=file_content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ):
        from agents.coder import run_coder_node

        await run_coder_node(state)

    mock_ainvoke.assert_called_once()
    messages: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    return messages


@pytest.mark.anyio
async def test_coder_system_head_is_byte_identical_across_two_turns() -> None:
    """PREFIX2 (coder) — same role, different target file/content, and a fresh
    nonce each turn; the cacheable head must still match byte-for-byte."""
    messages_a = await _run_coder_and_capture(
        _coder_state(_coder_step("a.py", "Refactor a.py")),
        file_content="def a():\n    return 1\n",
    )
    messages_b = await _run_coder_and_capture(
        _coder_state(_coder_step("b.py", "Refactor b.py")),
        file_content="def b():\n    return 2\n",
    )

    system_a = messages_a[0]["content"]
    system_b = messages_b[0]["content"]
    assert _DELIMITER_MARKER in system_a
    assert _DELIMITER_MARKER in system_b
    assert _head(system_a) == _head(system_b)
    assert system_a != system_b


@pytest.mark.anyio
async def test_coder_user_turn_carries_no_axiom_declaration_language() -> None:
    """SEAL2 (coder). The coder's system prompt previously carried NO axiom at
    all — build_boundary_declaration now closes that latent gap — so this also
    guards that the newly-added declaration didn't leak into the user turn."""
    messages = await _run_coder_and_capture(_coder_state(_coder_step("a.py", "Refactor a.py")))
    user_turn = messages[1]["content"]
    for phrase in _AXIOM_PHRASES:
        assert phrase not in user_turn, (
            f"axiom/declaration phrase {phrase!r} leaked into the user turn: {user_turn!r}"
        )


@pytest.mark.anyio
async def test_coder_degrade_path_still_declares_the_boundary() -> None:
    """DEGRADE1 (coder)."""
    with patch("agents.coder.resolve_context_budget", return_value=1):
        messages = await _run_coder_and_capture(_coder_state(_coder_step("a.py", "Refactor a.py")))

    system_content = messages[0]["content"]
    assert _DELIMITER_MARKER in system_content, (
        "boundary declaration must survive the identity-only degrade path"
    )


# =============================================================================
# agentic_cell — PREFIX2 (already correct; this is a pure regression lock)
# =============================================================================


def test_agentic_cell_system_row_is_byte_identical_across_iterations() -> None:
    """brain/agentic_cell.py never embedded a per-turn nonce in its leading
    system message — messages[0] is a hardcoded literal string. No code change
    was needed there; this locks the property in so a future edit can't
    silently reintroduce a per-iteration nonce."""
    from brain.agentic_cell import _build_messages

    base_state: Dict[str, Any] = {"user_input": "Fix the failing test.", "mission_spec": None}

    first_iteration = _build_messages({**base_state, "agentic_trajectory": []})
    second_iteration = _build_messages(
        {
            **base_state,
            "agentic_trajectory": [
                {"role": "system", "content": "ran pytest"},
                {"diagnostics": "1 failed, 2 passed"},
            ],
        }
    )

    assert first_iteration[0] == second_iteration[0]
    assert first_iteration[0]["role"] == "system"
    assert not _HEX32_RE.search(first_iteration[0]["content"])
