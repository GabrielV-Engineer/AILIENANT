# ailienant-core/tests/test_full_swarm.py
"""Phase 4.3 stage-2 — FULL_SWARM compilation tests.

These tests verify that build_full_swarm() compiles cleanly with the two
expected checkpointer shapes (real MemorySaver, None). End-to-end behaviour
of the inner nodes (researcher, planner, orchestrator, analyst) is covered
by their own per-agent test suites; this file only asserts the topology
contract surface.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

pytestmark = pytest.mark.anyio


def test_compiles_with_memorysaver() -> None:
    """Caller-supplied MemorySaver must compile and expose .ainvoke."""
    from brain.swarms import build_full_swarm

    app = build_full_swarm(MemorySaver())
    assert hasattr(app, "ainvoke")


def test_compiles_without_checkpointer() -> None:
    """Compilation must succeed with no checkpointer (None branch)."""
    from brain.swarms import build_full_swarm

    app = build_full_swarm(None)
    assert hasattr(app, "ainvoke")
