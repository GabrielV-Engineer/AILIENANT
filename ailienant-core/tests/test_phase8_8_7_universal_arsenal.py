# ailienant-core/tests/test_phase8_8_7_universal_arsenal.py
#
# Wave 6 universal arsenal — todo_write (net-new) + tool_search (cross-listed to all roles).
#
#   A — registration: register_universal_tools returns 1; todo_write present with ALL_ROLES.
#   B — all-roles RBAC: every role in ALL_ROLES can surface todo_write (and tool_search).
#   C — tier / PLAN survival: todo_write is READ_ONLY → admitted in PLAN mode.
#   D — _arun behavior: returns a JSON string; caps at 50; single-active invariant; validation.
#   E — reducer contract (anti-immortal-TODO): None keeps, [] clears, replace wins.
#   F — cross-list: tool_search.allowed_roles widened to ALL_ROLES; both visible to graph roles.

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Dict, List

import pytest
from pydantic import ValidationError

from brain.state import _merge_todos
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.control_tools import ALL_ROLES
from tools.meta_tools import register_meta_tools
from tools.universal_tools import (
    TodoItem,
    TodoWriteInput,
    TodoWriteTool,
    register_universal_tools,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# Fixtures — isolated store with deterministic fake embeddings
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8."""

    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag_887"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


def _by_name(store: ToolRAGStore) -> Dict[str, ToolSchema]:
    return {s.name: s for s in store.all_schemas()}


# =====================================================================
# A — registration
# =====================================================================


@pytest.mark.anyio
async def test_register_universal_tools_returns_one(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_universal_tools(store)
    assert count == 1
    assert "todo_write" in _by_name(store)


@pytest.mark.anyio
async def test_todo_write_role_set_is_all_roles(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_universal_tools(store)
    schema = _by_name(store)["todo_write"]
    assert schema.allowed_roles == ALL_ROLES
    assert schema.privilege_tier is ToolPrivilegeTier.READ_ONLY


# =====================================================================
# B — all-roles RBAC
# =====================================================================


@pytest.mark.anyio
async def test_todo_write_surfaces_for_every_role(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_universal_tools(store)
    await register_meta_tools(store)

    for role in ALL_ROLES:
        matches = await store.select_tools(
            "write my task list",
            active_role=role,
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {m.name for m in matches}
        assert "todo_write" in names, f"todo_write missing for role {role!r}"
        assert "tool_search" in names, f"tool_search missing for role {role!r}"


# =====================================================================
# C — tier / PLAN survival
# =====================================================================


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["core_dev", "planner"])
async def test_todo_write_survives_plan_mode(tmp_path: Path, role: str) -> None:
    store = _isolated_store(tmp_path)
    await register_universal_tools(store)

    matches = await store.select_tools(
        "publish todo list",
        active_role=role,
        session_mode=SessionPermissionMode.PLAN,
    )
    assert "todo_write" in {m.name for m in matches}


# =====================================================================
# D — _arun behavior
# =====================================================================


@pytest.mark.anyio
async def test_arun_returns_json_string_with_count(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    todos = [
        TodoItem(content="Read the spec", status="completed", active_form="Reading the spec"),
        TodoItem(content="Write the code", status="in_progress", active_form="Writing the code"),
        TodoItem(content="Run the tests", status="pending", active_form="Running the tests"),
    ]
    result = await tool._arun(todos=todos)

    assert isinstance(result, str)  # BaseTool serialization contract — never a raw dict
    decoded = json.loads(result)
    assert decoded["count"] == 3
    assert [t["status"] for t in decoded["agent_todos"]] == ["completed", "in_progress", "pending"]
    assert decoded["agent_todos"][0]["active_form"] == "Reading the spec"


@pytest.mark.anyio
async def test_arun_caps_at_fifty(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    todos = [
        TodoItem(content=f"task {i}", status="pending", active_form=f"doing task {i}")
        for i in range(120)
    ]
    decoded = json.loads(await tool._arun(todos=todos))
    assert decoded["count"] == 50
    assert decoded["agent_todos"][0]["content"] == "task 0"  # kept the FIRST 50


@pytest.mark.anyio
async def test_arun_enforces_single_active(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    todos = [
        TodoItem(content="a", status="in_progress", active_form="doing a"),
        TodoItem(content="b", status="in_progress", active_form="doing b"),
        TodoItem(content="c", status="in_progress", active_form="doing c"),
    ]
    decoded = json.loads(await tool._arun(todos=todos))
    statuses = [t["status"] for t in decoded["agent_todos"]]
    assert statuses == ["in_progress", "pending", "pending"]  # first kept, rest demoted


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError):
        TodoWriteInput.model_validate(
            {"todos": [{"content": "x", "status": "done", "active_form": "doing x"}]}
        )


def test_empty_content_rejected() -> None:
    with pytest.raises(ValidationError):
        TodoWriteInput.model_validate(
            {"todos": [{"content": "", "status": "pending", "active_form": "x"}]}
        )


def test_empty_active_form_rejected() -> None:
    with pytest.raises(ValidationError):
        TodoWriteInput.model_validate(
            {"todos": [{"content": "x", "status": "pending", "active_form": ""}]}
        )


# =====================================================================
# E — reducer contract (anti-immortal-TODO)
# =====================================================================


def test_merge_todos_explicit_empty_clears() -> None:
    prior = [{"content": "old", "status": "completed", "active_form": "doing old"}]
    # An explicit [] means "I finished — clear the panel"; it must NOT fall back to prior.
    assert _merge_todos(prior, []) == []


def test_merge_todos_none_keeps_prior() -> None:
    prior = [{"content": "old", "status": "pending", "active_form": "doing old"}]
    assert _merge_todos(prior, None) == prior


def test_merge_todos_replaces_and_handles_uninitialized() -> None:
    new = [{"content": "new", "status": "pending", "active_form": "doing new"}]
    assert _merge_todos(None, new) == new
    assert _merge_todos(None, None) == []


@pytest.mark.anyio
async def test_arun_payload_round_trips_through_reducer(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    todos = [TodoItem(content="t", status="pending", active_form="doing t")]
    decoded = json.loads(await tool._arun(todos=todos))
    merged = _merge_todos([], decoded["agent_todos"])
    assert merged == decoded["agent_todos"]


# =====================================================================
# F — cross-list
# =====================================================================


@pytest.mark.anyio
async def test_tool_search_cross_listed_to_all_roles(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_meta_tools(store)
    await register_universal_tools(store)

    schemas = _by_name(store)
    assert schemas["tool_search"].allowed_roles == ALL_ROLES

    # Both universal tools must be visible to a non-canonical graph-node role.
    for role in ("researcher", "planner"):
        matches = await store.select_tools(
            "discover tools and write my plan",
            active_role=role,
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {m.name for m in matches}
        assert {"tool_search", "todo_write"} <= names, f"missing universal tools for {role!r}"
