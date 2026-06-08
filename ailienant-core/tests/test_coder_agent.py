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

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep


def _fake_llm_response(content: str) -> Any:
    """Minimal stand-in for a litellm ModelResponse (resp.choices[0].message.content)."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


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
def _mock_coder_io() -> Any:
    """Isolate run_coder_node from I/O: WS broadcast, RAG, VFS read, and the LLM.

    Defaults: LLM returns an empty edit set, the file reads as simple Python, RAG is
    empty. Individual tests can nest their own patches to override these.
    """
    from core.vfs_middleware import VFSReadResult
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
        new=AsyncMock(return_value=_fake_llm_response("")),
    ):
        yield


# ── Test A — doc_manager tool whitelist ──────────────────────────────────────


def test_coder_agent_resolves_doc_manager_tool_whitelist() -> None:
    """doc_manager must NOT have BashTool; must have WriteFileTool + apply_patch."""
    from agents.roles import ROLE_REGISTRY

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
    from agents.roles import build_coder_system_prompt

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
        "pending_patches",   # Phase 7.9.B.16 — coder now proposes diffs
        "pending_contents",  # Phase 7.9.B.18 — full new content for the write pipeline
        "pending_base_hash", # Phase 7.9.B.18 — pre-edit hash for the stale guard
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


# ── Test E — a valid AtomicPatch edit produces a unified diff (Phase 7.9.B.16) ─


@pytest.mark.anyio
async def test_coder_produces_unified_diff_for_valid_edit() -> None:
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    content = "def calculate(x):\n    return x + 1\n"
    edit_blob = (
        "### EDIT calc.py\n"
        "<<<<<<< SEARCH\n"
        "    return x + 1\n"
        "=======\n"
        "    return x + 2\n"
        ">>>>>>> REPLACE\n"
    )
    step = _make_step(action="edit_file", target_file="calc.py", description="Bump increment.")
    state = _make_state(_make_mission([step]))

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response(edit_blob)),
    ):
        result = await run_coder_node(state)

    assert "pending_patches" in result
    assert "calc.py" in result["pending_patches"], result
    diff = result["pending_patches"]["calc.py"]
    assert "return x + 2" in diff and "return x + 1" in diff

    # Phase 7.9.B.18 — the coder also emits the full new content + a pre-edit hash.
    from agents.coder import content_hash
    assert result["pending_contents"]["calc.py"] == "def calculate(x):\n    return x + 2\n"
    assert result["pending_base_hash"]["calc.py"] == content_hash(content)


# ── Test F — SEARCH/REPLACE block parser ──────────────────────────────────────


def test_parse_single_edit() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits == [
        {"file_path": "main.py", "search_block": "    return 1", "replace_block": "    return 2"}
    ]


def test_parse_multiple_edits() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        "### EDIT b.py\n<<<<<<< SEARCH\ny = 3\n=======\ny = 4\n>>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert len(edits) == 2
    assert edits[0]["file_path"] == "a.py" and edits[0]["replace_block"] == "x = 2"
    assert edits[1]["file_path"] == "b.py" and edits[1]["search_block"] == "y = 3"


def test_parse_new_file_empty_search() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT new.py\n"
        "<<<<<<< SEARCH\n"
        "=======\n"
        "def hello():\n    return 'hi'\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == ""
    assert edits[0]["replace_block"] == "def hello():\n    return 'hi'"


def test_parse_tolerates_surrounding_prose() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "Sure, here is the edit you asked for:\n\n"
        "### EDIT main.py\n<<<<<<< SEARCH\na = 1\n=======\na = 2\n>>>>>>> REPLACE\n\n"
        "Let me know if you need anything else!\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits == [
        {"file_path": "main.py", "search_block": "a = 1", "replace_block": "a = 2"}
    ]


def test_parse_strips_border_blank_lines() -> None:
    from agents.coder import _parse_search_replace_blocks

    # Model padded the body with blank lines after the SEARCH marker and before
    # the divider; _clean_block must strip them so the EXACT match still lands.
    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "\n"
        "    return 1\n"
        "\n"
        "=======\n"
        "\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == "    return 1"
    assert edits[0]["replace_block"] == "    return 2"


def test_parse_peels_accidental_markdown_fence() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "```python\n"
        "    return 1\n"
        "```\n"
        "=======\n"
        "```\n"
        "    return 2\n"
        "```\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == "    return 1"
    assert edits[0]["replace_block"] == "    return 2"


def test_parse_keeps_internal_backticks() -> None:
    from agents.coder import _parse_search_replace_blocks

    # A body that merely CONTAINS a fence line internally (not wrapping it) must
    # not be corrupted — the first line is real code, not a fence opener.
    text = (
        "### EDIT doc.py\n"
        "<<<<<<< SEARCH\n"
        "x = 1\n"
        "=======\n"
        'README = """\n```\ncode\n```\n"""\n'
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert "```" in edits[0]["replace_block"]
    assert edits[0]["replace_block"].startswith('README = """')


@pytest.mark.anyio
async def test_coder_exact_patch_survives_leading_blank_line() -> None:
    """End-to-end: a model-padded SEARCH block still lands an EXACT patch (no fuzzy)."""
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    content = "def calculate(x):\n    return x + 1\n"
    # Blank line right after the SEARCH marker — _clean_block must remove it so the
    # anchor matches the file verbatim through apply_search_replace Pass 1.
    edit_blob = (
        "### EDIT calc.py\n"
        "<<<<<<< SEARCH\n"
        "\n"
        "    return x + 1\n"
        "=======\n"
        "    return x + 2\n"
        ">>>>>>> REPLACE\n"
    )
    step = _make_step(action="edit_file", target_file="calc.py", description="Bump increment.")
    state = _make_state(_make_mission([step]))

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response(edit_blob)),
    ):
        result = await run_coder_node(state)

    assert result["pending_contents"]["calc.py"] == "def calculate(x):\n    return x + 2\n"
