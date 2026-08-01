"""Phase 12.3 — Remaining Integration DEBTs Sprint.

Covers the corrected DEBT-049 (SkillInvokeTool's "semantic matching disabled" premise
was false; skill_invoke is architecturally unreachable via resolve_tools() for a
structural reason — role-scope disjointness from its only consumer — not a gateway
duplicate) and the closed DEBT-054 (the agentic cell's todo_write fallback path now
promotes its payload onto the agent_todos state channel and streams it over WS, with
an event-loop parse ceiling and emit idempotence guarding the new surface).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.tools import BaseTool

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall as CellToolCall, _emit_agent_todos_if_changed, run_agentic_cell_node
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher, promote_tool_state
from core.tool_rag import ToolRAGStore, ToolSchema
from core.tool_registry import _INTENTIONALLY_UNREGISTERED, all_registrable_names
from shared.config import MAX_JSON_PARSE_CHARS, MAX_OBSERVATION_CHARS
from shared.rbac import PermissionMode
from tools.gateway_tools import SkillInvokeTool

from tests.test_phase7_19_2_agentic_cell import (
    StubAdapter,
    StubSession,
    StubSyncSurface,
    _base_state,
    _config,
    _reasoner_from,
)
from tests.test_phase7_19_4_cell_dispatcher import CapturingCellDispatcher


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Part A — DEBT-049 (record correction + regression lock)
# ══════════════════════════════════════════════════════════════════════════════


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


def test_skill1_embed_fn_none_still_reaches_the_default_embedder(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression lock: DEBT-049's premise (embed_fn=None disables matching) is
    false. resolve_active_skills falls back to core.tool_rag._default_embed_fn —
    spied on directly here, rather than patching resolve_active_skills itself,
    which would only prove the mock works, not that the fallback wiring is intact."""
    from core import db as catalog_db
    from core import skill_resolver

    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog_skill1.sqlite"))
    spy = AsyncMock(return_value=[1.0, 0.0])

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_skill("s1", "Match", "body", description="candidate skill")
        with patch("core.tool_rag._default_embed_fn", new=spy):
            result = await skill_resolver.resolve_active_skills(
                user_input="query", workspace_root="/ws", invoked_skill_id=None, embed_fn=None,
            )
        assert spy.await_count >= 1, "embed_fn=None must still reach an embedder"
        assert [s["name"] for s in result] == ["Match"]

    asyncio.run(_run())


def test_skill2_tool_auto_matches_via_real_resolver(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SkillInvokeTool with no skill_id, driven through the REAL resolver (not a
    mocked resolve_active_skills as in test_phase8_8_6_gateway_arsenal.py) —
    proves the tool-to-resolver wire, not just the resolver in isolation."""
    from core import db as catalog_db

    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog_skill2.sqlite"))

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_skill("s1", "AutoMatch", "body", description="candidate skill")
        spy = AsyncMock(return_value=[1.0, 0.0])
        with patch("core.tool_rag._default_embed_fn", new=spy):
            tool = SkillInvokeTool()
            result = await tool._arun(user_input="task", workspace_root="/ws")
        payload = json.loads(result)
        assert payload["count"] == 1
        assert payload["skills"][0]["name"] == "AutoMatch"
        assert spy.await_count >= 1

    asyncio.run(_run())


def test_skill3_skill_invoke_excluded_for_role_scope_not_gateway() -> None:
    reason = _INTENTIONALLY_UNREGISTERED["skill_invoke"]
    assert "gateway/handlers.py" not in reason, (
        "skill_invoke has no gateway/catalog.py counterpart — the exclusion "
        "reason must not claim a gateway duplicate"
    )
    assert "dispatch loop" in reason or "disjoint" in reason
    assert "skill_invoke" not in all_registrable_names()


def test_skill4_skill_roles_disjoint_from_control_roles() -> None:
    """The structural fact that makes skill_invoke's exclusion correct: its
    allowed_roles never overlaps the WBS coder roles resolve_tools()'s only
    consumer (the agentic cell) runs under. If a future phase gives a WBS step
    a planner/orchestrator target_role, this row fails and forces the exclusion
    to be re-decided instead of silently going stale."""
    from tools.control_tools import _CONTROL_ROLES
    from tools.gateway_tools import _SKILL_ROLES

    assert not (_SKILL_ROLES & _CONTROL_ROLES)


def test_skill5_no_stale_disabled_claim_or_debt_reference() -> None:
    tool = SkillInvokeTool()
    doc = SkillInvokeTool.__doc__ or ""
    for text in (doc, tool.description):
        assert "disabled" not in text.lower()
        assert "DEBT-" not in text


# ══════════════════════════════════════════════════════════════════════════════
# Part B — DEBT-054 (channel + WS promotion)
# ══════════════════════════════════════════════════════════════════════════════


def test_todo1_promote_known_and_unknown_tool() -> None:
    raw = json.dumps({
        "agent_todos": [{"content": "a", "status": "pending", "active_form": "Doing a"}],
        "count": 1,
    })
    result = promote_tool_state("todo_write", raw)
    assert result == {"agent_todos": [{"content": "a", "status": "pending", "active_form": "Doing a"}]}
    assert promote_tool_state("not_allowlisted", raw) is None


def test_todo2_malformed_payload_returns_none() -> None:
    assert promote_tool_state("todo_write", "not json at all") is None
    assert promote_tool_state("todo_write", json.dumps({"wrong_key": []})) is None
    assert promote_tool_state("todo_write", json.dumps({"agent_todos": "not-a-list"})) is None


def test_todo3_large_payload_promotes_but_observation_truncates() -> None:
    items = [{"content": "x" * 200, "status": "pending", "active_form": "y" * 50} for _ in range(20)]
    raw = json.dumps({"agent_todos": items, "count": len(items)})
    # Sanity-check the fixture itself sits between the two ceilings this row exists
    # to distinguish, rather than assuming a guessed size clears both.
    assert MAX_OBSERVATION_CHARS < len(raw) < MAX_JSON_PARSE_CHARS

    class _BigTodoTool(BaseTool):
        name: str = "todo_write"
        description: str = "test"

        def _run(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _arun(self, **kwargs: Any) -> str:
            return raw

    tools = {
        "todo_write": RegisteredTool(
            tool=_BigTodoTool(), tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
        )
    }
    dispatcher = ToolDispatcher(
        tools, active_role="core_dev", session_mode=SessionPermissionMode.DEFAULT,
        state={}, agent_permission=PermissionMode.READ_ONLY,
    )

    async def _run() -> Any:
        return await dispatcher.dispatch(ToolCall("todo_write", {}))

    result = asyncio.run(_run())

    assert result.executed
    assert len(result.observation) <= MAX_OBSERVATION_CHARS + len("\n…[truncated]")
    assert result.observation.endswith("[truncated]")
    assert result.state_delta is not None
    assert len(result.state_delta["agent_todos"]) == 20, "the delta must not inherit the observation's truncation"


def test_todo4_delta_carries_agent_todos_and_merge_todos_applies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    asyncio.run(store.register_schema(ToolSchema(
        name="todo_write", description="test", json_schema="{}",
        privilege_tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
    )))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    todos_payload = [{"content": "Add tests", "status": "in_progress", "active_form": "Adding tests"}]
    reasoner = _reasoner_from([[CellToolCall("todo_write", {"todos": todos_payload})]])
    state = _base_state()

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert "agent_todos" in delta
    assert delta["agent_todos"] == todos_payload

    from brain.state import _merge_todos

    merged = _merge_todos(None, delta["agent_todos"])
    assert merged == delta["agent_todos"]
    cleared = _merge_todos(merged, [])
    assert cleared == [], "an explicit empty write must clear, never be read as 'no opinion'"


def test_todo5_todo_write_before_deferred_run_terminal_survives_in_defer_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    asyncio.run(store.register_schema(ToolSchema(
        name="todo_write", description="test", json_schema="{}",
        privilege_tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
    )))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    todos_payload = [{"content": "Write tests", "status": "pending", "active_form": "Writing tests"}]
    # todo_write listed BEFORE run_terminal so it executes before the HITL deferral's
    # early return — the exact ordering this row exists to certify.
    reasoner = _reasoner_from([[
        CellToolCall("todo_write", {"todos": todos_payload}),
        CellToolCall("run_terminal", {"command": "pytest -q"}),
    ]])
    state = _base_state(session_permission_mode="DEFAULT")  # DEFAULT -> EXECUTE resolves to HITL

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert delta.get("pending_exec_command") == "pytest -q", "the deferral must actually have happened"
    assert session.run_calls == [], "run_terminal must not have run"
    assert delta.get("agent_todos") == todos_payload


def test_todo6_multiple_todo_write_calls_collapse_to_one_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    asyncio.run(store.register_schema(ToolSchema(
        name="todo_write", description="test", json_schema="{}",
        privilege_tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
    )))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    first = [{"content": "a", "status": "pending", "active_form": "A"}]
    second = [{"content": "a", "status": "in_progress", "active_form": "A"}]
    reasoner = _reasoner_from([[
        CellToolCall("todo_write", {"todos": first}),
        CellToolCall("todo_write", {"todos": second}),
    ]])
    cap = CapturingCellDispatcher()
    state = _base_state()

    asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner, cell_dispatcher=cap)))

    todo_events = [e for e in cap.events if e[0] == "agent_todos"]
    assert len(todo_events) == 1, "two todo_write calls in one turn must collapse to one emit"
    assert todo_events[0][1]["todos"] == second, "last write wins within the iteration"


def test_todo7_oversized_payload_never_reaches_json_loads() -> None:
    huge = "x" * (MAX_JSON_PARSE_CHARS + 1)
    with patch("core.tool_dispatch.json.loads") as mock_loads:
        result = promote_tool_state("todo_write", huge)
    assert result is None
    mock_loads.assert_not_called()


def test_todo8_unchanged_list_suppresses_emit() -> None:
    cap = CapturingCellDispatcher()
    existing = [{"content": "a", "status": "pending", "active_form": "A"}]
    state = {"agent_todos": existing}

    async def _run() -> None:
        # Deep-equal but a DIFFERENT list object — the comparison must be by value.
        await _emit_agent_todos_if_changed(cap, state, 0, {"agent_todos": list(existing)})
        assert cap.events == []

        changed = [{"content": "a", "status": "completed", "active_form": "A"}]
        await _emit_agent_todos_if_changed(cap, state, 0, {"agent_todos": changed})
        assert len(cap.events) == 1

    asyncio.run(_run())
