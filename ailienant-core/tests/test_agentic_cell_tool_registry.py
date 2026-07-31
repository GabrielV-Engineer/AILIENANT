"""Division 8.18.2 — CoderAgent tool activation via the agentic cell's additive fallback.

Covers the end-to-end path the checkpoint gate cares about: a tool name outside
the 3 hardcoded CELL_TOOLS primitives (run_terminal/read_file_ast/apply_granular_edit)
resolves through core/tool_registry.py and executes via core/tool_dispatch.py's
ToolDispatcher — without touching any of the 3 primitives' own dispatch branches.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall, run_agentic_cell_node
from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema

from tests.test_phase7_19_2_agentic_cell import (
    StubAdapter,
    StubSession,
    StubSyncSurface,
    _base_state,
    _config,
    _reasoner_from,
)


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


def test_fallback_tool_executes_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously-orphaned tool (todo_write) is selected, constructed, and run —
    the exact capability gap Division 8.18 exists to close."""
    store = _isolated_store()
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.READ_ONLY,
                allowed_roles=frozenset({"core_dev"}),
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("todo_write", {"todos": []})]])
    state = _base_state()

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    trajectory = delta["agentic_trajectory"]
    fallback_msgs = [m for m in trajectory if isinstance(m, dict) and m.get("role") == "system" and "[todo_write]" in m.get("content", "")]
    assert fallback_msgs, f"expected a [todo_write] observation in trajectory, got: {trajectory}"
    assert "not found" not in fallback_msgs[0]["content"]
    assert "DENIED" not in fallback_msgs[0]["content"]
    # The 3 primitives' own dispatch state is untouched by the fallback branch.
    assert session.run_calls == []


def test_fallback_tool_invisible_outside_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool whose schema doesn't list the active role never reaches the model in
    the first place — RBAC is enforced at catalog-selection time (select_tools's
    allowed_roles pre-filter), not only at dispatch time. A reasoner that still
    (incorrectly, or adversarially) names it gets ToolDispatcher's own lookup-miss
    observation, since the tool was never in the resolved set to begin with —
    the same safe, non-crashing outcome as any unrecognized name, just reached via
    the catalog-level gate rather than the dispatch-level role check."""
    store = _isolated_store()
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.READ_ONLY,
                allowed_roles=frozenset({"secops"}),  # NOT core_dev
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("todo_write", {"todos": []})]])
    state = _base_state()  # no mission_spec/current_step_id -> active_role defaults to core_dev

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    trajectory = delta["agentic_trajectory"]
    fallback_msgs = [m for m in trajectory if isinstance(m, dict) and "[todo_write]" in m.get("content", "")]
    assert fallback_msgs
    assert "not found" in fallback_msgs[0]["content"]
    assert "todo_write" not in fallback_msgs[0]["content"].split("Available tools:")[1]


def test_unrecognized_fallback_name_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name that isn't in the catalog at all degrades to a clear observation,
    never a crash — ToolDispatcher.dispatch()'s own lookup-miss contract."""
    monkeypatch.setattr("core.tool_rag.tool_rag_store", _isolated_store())  # empty catalog

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("not_a_real_tool", {})]])
    state = _base_state()

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    trajectory = delta["agentic_trajectory"]
    fallback_msgs = [m for m in trajectory if isinstance(m, dict) and "[not_a_real_tool]" in m.get("content", "")]
    assert fallback_msgs
    assert "not found" in fallback_msgs[0]["content"]


def test_three_primitives_unaffected_when_catalog_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty catalog (the common case before main.py's lifespan populates it, or
    in any isolated test) must not change run_terminal's existing behavior at all."""
    monkeypatch.setattr("core.tool_rag.tool_rag_store", _isolated_store())

    session = StubSession(exit_codes=[0], outputs=[b"1 passed\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("run_terminal", {"command": "pytest"})]])
    state = _base_state()

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert session.run_calls == ["pytest"]
    assert delta["agentic_trajectory"][0]["status"] == "green"
