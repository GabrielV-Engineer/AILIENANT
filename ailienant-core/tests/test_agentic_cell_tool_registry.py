"""Division 8.18.2 — CoderAgent tool activation via the agentic cell's additive fallback.

Covers the end-to-end path the checkpoint gate cares about: a tool name outside
the 3 hardcoded CELL_TOOLS primitives (run_terminal/read_file_ast/apply_granular_edit)
resolves through core/tool_registry.py and executes via core/tool_dispatch.py's
ToolDispatcher — without touching any of the 3 primitives' own dispatch branches.

Also covers DEBT-129: a HITL-tier fallback tool defers (via the new
``pending_tool_call`` channel) instead of being denied-with-report, and the
tool-call-approval phase resolves and dispatches it exactly once on resume.
"""
from __future__ import annotations

import asyncio
from typing import Any, List
from unittest.mock import AsyncMock

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


# ── DEBT-129: HITL defer + tool-call-approval phase ─────────────────────────────


def test_fallback_hitl_tool_defers_and_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fallback tool whose (mode, tier) resolves to HITL defers instead of the
    ToolDispatcher's own no-channel-wired deny-with-report — it sets
    pending_tool_call and executes nothing this iteration."""
    store = _isolated_store()
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                # WRITE tier under DEFAULT (-> CAUTIOUS) resolves to HITL.
                privilege_tier=ToolPrivilegeTier.WRITE,
                allowed_roles=frozenset({"core_dev"}),
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    todo_args = {
        "todos": [{"content": "write tests", "status": "pending", "active_form": "Writing tests"}]
    }
    reasoner = _reasoner_from([[ToolCall("todo_write", todo_args)]])
    state = _base_state(session_permission_mode="DEFAULT")

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    pending = delta.get("pending_tool_call")
    assert pending is not None
    # DEBT-133: activity_ref is minted for the Glass-Box Timeline row opened
    # before the interrupt — additive, asserted for presence/shape only (the
    # value itself is a fresh uuid4 hex each run).
    assert pending["name"] == "todo_write"
    assert pending["args"] == todo_args
    assert isinstance(pending.get("activity_ref"), str) and pending["activity_ref"]
    trajectory = delta["agentic_trajectory"]
    assert trajectory[0]["status"] == "continue"
    # The tool never actually ran: no state promotion (todo_write's own
    # allowlisted agent_todos write) and no run_terminal call on the session.
    assert "agent_todos" not in delta
    assert session.run_calls == []


def test_tool_call_approval_phase_dispatches_exactly_once_on_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume with pending_tool_call set: approval is the FIRST action, then the
    tool executes exactly once — the reasoner is NOT re-invoked, mirroring the
    exec-approval phase's own contract."""
    store = _isolated_store()
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.WRITE,
                allowed_roles=frozenset({"core_dev"}),
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    approve = AsyncMock(return_value=True)
    reasoner_calls = {"n": 0}

    async def _counting_reasoner(_messages: Any) -> List[ToolCall]:
        reasoner_calls["n"] += 1
        return [ToolCall("todo_write", {"todos": []})]

    state = _base_state(
        session_permission_mode="DEFAULT",
        pending_tool_call={
            "name": "todo_write",
            "args": {
                "todos": [
                    {"content": "write tests", "status": "pending", "active_form": "Writing tests"}
                ]
            },
        },
    )
    config = _config(adapter, _counting_reasoner, cell_tool_approval_fn=approve)

    delta = asyncio.run(run_agentic_cell_node(state, config))

    approve.assert_awaited_once()
    assert delta.get("pending_tool_call") is None
    assert reasoner_calls["n"] == 0, "the reasoner must NOT run in the tool-call-approval phase"
    # todo_write's observation promotes to agent_todos — proof the real dispatch
    # actually executed (not merely reported "not found"/"DENIED") — and with the
    # ORIGINAL deferred args (one item), not the counting reasoner's ({} which is
    # never invoked in the first place, asserted above).
    assert delta.get("agent_todos") == [
        {"content": "write tests", "status": "pending", "active_form": "Writing tests"}
    ]
    trajectory = delta["agentic_trajectory"]
    observation_msgs = [
        m for m in trajectory if isinstance(m, dict) and "[todo_write]" in m.get("content", "")
    ]
    assert observation_msgs, f"expected a [todo_write] observation, got: {trajectory}"
    assert "not found" not in observation_msgs[0]["content"]


def test_tool_call_approval_phase_resolves_by_name_not_by_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approved tool is resolved via all_schemas() + exact name match, never
    by re-running the intent-ranked select_tools() — proven here by registering
    OTHER schemas that would dominate a real ranking, while the deferred name
    still resolves and dispatches correctly."""
    store = _isolated_store()

    async def _register(name: str, role: str = "core_dev") -> None:
        await store.register_schema(
            ToolSchema(
                name=name,
                description=f"decoy {name}",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.READ_ONLY,
                allowed_roles=frozenset({role}),
            )
        )

    asyncio.run(_register("security_audit"))
    asyncio.run(_register("validate_ast"))
    asyncio.run(_register("tool_search"))
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.WRITE,
                allowed_roles=frozenset({"core_dev"}),
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    approve = AsyncMock(return_value=True)

    async def _never_called(_messages: Any) -> List[ToolCall]:
        raise AssertionError("reasoner must not run during the approval phase")

    state = _base_state(
        session_permission_mode="DEFAULT",
        pending_tool_call={"name": "todo_write", "args": {"todos": []}},
    )
    config = _config(adapter, _never_called, cell_tool_approval_fn=approve)

    delta = asyncio.run(run_agentic_cell_node(state, config))

    assert delta.get("pending_tool_call") is None
    trajectory = delta["agentic_trajectory"]
    observation_msgs = [
        m for m in trajectory if isinstance(m, dict) and "[todo_write]" in m.get("content", "")
    ]
    assert observation_msgs
    assert "not found" not in observation_msgs[0]["content"]


def test_tool_call_approval_denied_reports_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _isolated_store()
    asyncio.run(
        store.register_schema(
            ToolSchema(
                name="todo_write",
                description="Write your task TODO list to shared state.",
                json_schema="{}",
                privilege_tier=ToolPrivilegeTier.WRITE,
                allowed_roles=frozenset({"core_dev"}),
            )
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    deny = AsyncMock(return_value=False)
    state = _base_state(
        session_permission_mode="DEFAULT",
        pending_tool_call={"name": "todo_write", "args": {"todos": []}},
    )
    config = _config(adapter, _reasoner_from([[]]), cell_tool_approval_fn=deny)

    delta = asyncio.run(run_agentic_cell_node(state, config))

    deny.assert_awaited_once()
    assert delta.get("pending_tool_call") is None
    assert "TOOL_CALL_HITL_DENIED" in (delta.get("security_flags") or [])
    denial_msgs = [
        m
        for m in delta["agentic_trajectory"]
        if isinstance(m, dict) and "not approved" in m.get("content", "")
    ]
    assert denial_msgs


def test_tool_call_approval_unresolvable_name_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog no longer has the deferred name (e.g. changed across a
    restart) — reported as an observation, channel cleared, never a crash."""
    monkeypatch.setattr("core.tool_rag.tool_rag_store", _isolated_store())  # empty catalog

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    approve = AsyncMock(return_value=True)
    state = _base_state(
        session_permission_mode="DEFAULT",
        pending_tool_call={"name": "ghost_tool", "args": {}},
    )
    config = _config(adapter, _reasoner_from([[]]), cell_tool_approval_fn=approve)

    delta = asyncio.run(run_agentic_cell_node(state, config))

    assert delta.get("pending_tool_call") is None
    msgs = [
        m
        for m in delta["agentic_trajectory"]
        if isinstance(m, dict) and "no longer resolves" in m.get("content", "")
    ]
    assert msgs
