"""Phase 3.4.2 — Unit tests for Session Delta Aggregator (Pre-Dream Reflection).

DoD: proves that session_delta is correctly populated in the state after
a simulated failed compilation.
"""
from typing import Any

import pytest

from brain.nodes.aggregator_node import (
    _extract_blockers,
    _extract_context_state,
    _format_delta,
    run_session_delta_aggregator_node,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMULATED_COMPILATION_STATE = {
    "task_id": "test-task-001",
    "messages": [
        {"role": "user",      "content": "refactor auth module"},
        {"role": "assistant", "content": "Sure, I'll start with the auth module."},
        {"role": "user",      "content": "also add type hints to all functions"},
        {"role": "assistant", "content": "Type hints added."},
        {"role": "user",      "content": "run mypy and fix any errors"},
    ],
    "terminal_output": (
        "mypy agents/auth.py --strict\n"
        "agents/auth.py:42: error: Argument 1 to 'login' has incompatible type 'str'; expected 'bytes'\n"
        "agents/auth.py:87: error: Missing return statement\n"
        "Found 2 errors in 1 file (checked 1 source file)\n"
    ),
    "vfs_buffer": {
        "agents/auth.py":         {"content": "# modified", "document_version_id": "v2"},
        "agents/auth_helpers.py": {"content": "# modified", "document_version_id": "v1"},
    },
}


# ---------------------------------------------------------------------------
# Core DoD tests — session_delta populated after simulated failed compilation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_session_delta_key_present() -> None:
    """run_session_delta_aggregator_node returns a dict with 'session_delta'."""
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    assert "session_delta" in result


@pytest.mark.anyio
async def test_session_delta_is_non_empty_string() -> None:
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    delta: str = result["session_delta"]
    assert isinstance(delta, str) and delta.strip()


@pytest.mark.anyio
async def test_session_delta_header_present() -> None:
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    assert result["session_delta"].startswith("### SESSION DELTA")


@pytest.mark.anyio
async def test_session_delta_contains_all_sections() -> None:
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    delta: str = result["session_delta"]
    assert "- INTENT:" in delta
    assert "- BLOCKERS:" in delta
    assert "- CONTEXT_STATE:" in delta


@pytest.mark.anyio
async def test_compilation_errors_appear_in_blockers() -> None:
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    delta: str = result["session_delta"]
    assert "error" in delta.lower()


@pytest.mark.anyio
async def test_dirty_buffers_appear_in_context_state() -> None:
    result = await run_session_delta_aggregator_node(SIMULATED_COMPILATION_STATE)
    delta: str = result["session_delta"]
    assert "agents/auth.py" in delta


@pytest.mark.anyio
async def test_max_length_enforced() -> None:
    long_state = {
        **SIMULATED_COMPILATION_STATE,
        "terminal_output": "error: " + "x" * 5000,
    }
    result = await run_session_delta_aggregator_node(long_state)
    delta: str = result["session_delta"]
    assert len(delta) <= 500 * 4 + 3  # _MAX_CHARS + len("...")


@pytest.mark.anyio
async def test_empty_state_produces_valid_delta() -> None:
    """Aggregator must not raise on a completely empty state dict."""
    result = await run_session_delta_aggregator_node({})
    delta: str = result["session_delta"]
    assert "### SESSION DELTA" in delta
    assert "None." in delta or "No " in delta


@pytest.mark.anyio
async def test_no_messages_uses_no_intent_fallback() -> None:
    state = {**SIMULATED_COMPILATION_STATE, "messages": []}
    result = await run_session_delta_aggregator_node(state)
    assert "No prior user intent recorded." in result["session_delta"]


# ---------------------------------------------------------------------------
# Unit tests for pure helper functions
# ---------------------------------------------------------------------------


def test_extract_blockers_returns_none_on_clean_output() -> None:
    assert _extract_blockers("Build succeeded. All tests passed.") == "None."


def test_extract_blockers_returns_none_on_empty() -> None:
    assert _extract_blockers("") == "None."
    assert _extract_blockers("   ") == "None."


def test_extract_blockers_captures_error_lines() -> None:
    output = "line1\nerror: bad type\nline3\nwarning: unused var\n"
    result = _extract_blockers(output)
    assert "error: bad type" in result
    assert "warning: unused var" in result


def test_extract_blockers_deduplicates() -> None:
    output = "error: same\nerror: same\nerror: same\n"
    result = _extract_blockers(output)
    assert result.count("error: same") == 1


def test_extract_context_state_empty_buffer() -> None:
    assert _extract_context_state({}) == "No staged files."


def test_extract_context_state_lists_paths() -> None:
    buf: dict[str, Any] = {"src/a.py": {}, "src/b.py": {}}
    result = _extract_context_state(buf)
    assert "src/a.py" in result
    assert "src/b.py" in result


def test_extract_context_state_caps_at_ten() -> None:
    buf: dict[str, Any] = {f"file_{i}.py": {} for i in range(15)}
    result = _extract_context_state(buf)
    assert "+5 more" in result


def test_format_delta_structure() -> None:
    delta = _format_delta("Refactor auth", "Missing return statement", "agents/auth.py")
    assert delta.startswith("### SESSION DELTA")
    assert "- INTENT: Refactor auth" in delta
    assert "- BLOCKERS: Missing return statement" in delta
    assert "- CONTEXT_STATE: agents/auth.py" in delta


def test_format_delta_truncates_at_max_chars() -> None:
    long_intent = "x" * 3000
    delta = _format_delta(long_intent, "none", "none")
    assert len(delta) <= 500 * 4 + 3
    assert delta.endswith("...")
