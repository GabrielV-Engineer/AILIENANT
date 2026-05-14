# ailienant-core/tests/test_guardrails.py
#
# DoD: pytest tests/test_guardrails.py -v must pass with 0 failures.

import asyncio
from typing import List, Tuple
from unittest.mock import AsyncMock

import pytest

from brain.guardrails import MAX_RETRIES, run_validate_output_node, route_after_validation
from core.io_coalescer import IOCoalescer, _MASS_THRESHOLD, _UNLINK_SENTINEL


# ---------------------------------------------------------------------------
# BranchSwitchHandler — IOCoalescer unlink + mass detection tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unlink_events_dispatched_before_updates() -> None:
    """Mix of unlinks and updates: all unlinks must appear before any update in dispatch order."""
    dispatch_log: List[Tuple[str, str]] = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_log.append((fp, content))

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)

    # Submit interleaved: update, unlink, update, unlink, update
    coalescer.submit("a.py", "content_a", "")
    coalescer.submit_unlink("del1.py", "")
    coalescer.submit("b.py", "content_b", "")
    coalescer.submit_unlink("del2.py", "")
    coalescer.submit("c.py", "content_c", "")

    await asyncio.sleep(0.7)

    # Verify: all sentinel dispatches appear before any non-sentinel dispatch
    sentinel_indices = [i for i, (_, c) in enumerate(dispatch_log) if c == _UNLINK_SENTINEL]
    update_indices = [i for i, (_, c) in enumerate(dispatch_log) if c != _UNLINK_SENTINEL]

    assert len(sentinel_indices) == 2
    assert len(update_indices) == 3
    assert max(sentinel_indices) < min(update_indices), (
        "All unlink dispatches must precede all update dispatches"
    )


@pytest.mark.anyio
async def test_mass_event_triggers_mass_handler() -> None:
    """Submit _MASS_THRESHOLD + 50 updates → mass_handler called, dispatch_fn NOT called."""
    dispatch_calls: list = []
    mass_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append(fp)

    async def mock_mass_handler(pid: str) -> None:
        mass_calls.append(pid)

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)
    coalescer.register_mass_handler(mock_mass_handler)

    for i in range(_MASS_THRESHOLD + 50):
        coalescer.submit(f"file_{i}.py", f"content_{i}", "proj-1")

    await asyncio.sleep(0.7)

    assert len(dispatch_calls) == 0, "dispatch_fn must NOT be called for mass events"
    assert len(mass_calls) >= 1, "mass_handler must be called at least once"
    assert mass_calls[0] == "proj-1"


@pytest.mark.anyio
async def test_mass_handler_not_called_for_small_batch() -> None:
    """Submit 50 updates (below threshold) → dispatch_fn called 50×, mass_handler NOT called."""
    dispatch_calls: list = []
    mass_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append(fp)

    async def mock_mass_handler(pid: str) -> None:
        mass_calls.append(pid)

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)
    coalescer.register_mass_handler(mock_mass_handler)

    for i in range(50):
        coalescer.submit(f"file_{i}.py", f"content_{i}", "")

    await asyncio.sleep(0.7)

    assert len(dispatch_calls) == 50
    assert len(mass_calls) == 0


@pytest.mark.anyio
async def test_unlink_purges_via_dispatch_fn() -> None:
    """submit_unlink() must call dispatch_fn with _UNLINK_SENTINEL as content."""
    dispatch_calls: list = []

    async def mock_dispatch(fp: str, content: str, pid: str) -> None:
        dispatch_calls.append((fp, content, pid))

    coalescer = IOCoalescer()
    coalescer.register_dispatch(mock_dispatch)

    coalescer.submit_unlink("removed.py", "proj-x")

    await asyncio.sleep(0.7)

    assert len(dispatch_calls) == 1
    fp, content, pid = dispatch_calls[0]
    assert fp == "removed.py"
    assert content == _UNLINK_SENTINEL
    assert pid == "proj-x"


# ---------------------------------------------------------------------------
# OutputGuardrailNode — validation + retry tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_guardrail_passes_on_valid_state() -> None:
    """Valid state (vfs_buffer={}) → guardrail_failed=False, validation_feedback=None."""
    state = {
        "vfs_buffer": {},
        "current_step_id": 1,
        "target_role": "Refactor",
        "retry_count": 0,
    }
    result = await run_validate_output_node(state)
    assert result.get("guardrail_failed") is False
    assert result.get("validation_feedback") is None


@pytest.mark.anyio
async def test_guardrail_fails_and_increments_retry() -> None:
    """State with vfs_buffer=None (invalid type) → guardrail_failed=True, retry_count=1."""
    state = {
        "vfs_buffer": None,   # violates Dict[str, Any] contract
        "current_step_id": 1,
        "target_role": "Refactor",
        "retry_count": 0,
    }
    result = await run_validate_output_node(state)
    assert result.get("guardrail_failed") is True
    assert result.get("retry_count") == 1
    assert result.get("validation_feedback") is not None
    assert "[GUARDRAIL ERROR" in result["validation_feedback"]


@pytest.mark.anyio
async def test_guardrail_stops_at_max_retries() -> None:
    """retry_count already at MAX_RETRIES → guardrail_failed=False regardless of invalid output."""
    state = {
        "vfs_buffer": None,   # still invalid
        "current_step_id": 1,
        "target_role": "Refactor",
        "retry_count": MAX_RETRIES,
    }
    result = await run_validate_output_node(state)
    # After max retries, guardrail clears the flag so the graph can route to END
    assert result.get("guardrail_failed") is False


def test_route_after_validation_routes_to_coder_on_failure() -> None:
    """route_after_validation returns 'coder_agent' when guardrail_failed=True."""
    from langgraph.graph import END
    state = {"guardrail_failed": True}
    assert route_after_validation(state) == "coder_agent"


def test_route_after_validation_routes_to_end_on_success() -> None:
    """route_after_validation returns END when guardrail_failed=False."""
    from langgraph.graph import END
    state = {"guardrail_failed": False}
    assert route_after_validation(state) == END
