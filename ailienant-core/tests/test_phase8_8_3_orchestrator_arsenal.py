"""Wave 3 Orchestrator Arsenal gate — sibling-file checkpoint.

DoD:
  - get_wbs_status, emit_hitl_request, ask_user_question, toggle_plan_mode,
    read_token_ledger all carry "orchestrator" in allowed_roles.
  - select_tools(active_role="orchestrator") surfaces all 5; all are READ_ONLY and
    survive PLAN session mode.
  - Negative RBAC: vcs_manager/analyst do NOT get the 2 orchestrator-only net-new
    tools; analyst STILL gets read_token_ledger after the role union (no regression).
  - GetWBSStatusTool: aggregate counts + active_step; include_steps toggle; missing
    mission -> no_mission; 250-step cap -> truncated; tasks=None -> no TypeError.
  - EmitHITLRequestTool: emits canonical flag + deterministic request_id; idempotent
    within state AND across a fresh (dropped) state; sanitizes injected colons/newlines;
    empty-field guard refuses to emit.
  - Wire-in: ask_user_question/toggle_plan_mode keep all 8 canonical roles AND gain
    orchestrator (no role dropped).
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from brain.state import MissionSpecification, WBSStep
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.token_ledger import TokenLedger
from core.tool_rag import ToolRAGStore
from tools.analyst_tools import register_analyst_tools
from tools.control_tools import _CONTROL_ROLES, register_control_tools
from tools.orchestrator_tools import (
    _WBS_MAX_STEPS,
    EmitHITLRequestTool,
    GetWBSStatusTool,
    register_orchestrator_tools,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
        store_path=str(tmp_path / "tool_rag_883"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


async def _register_all(store: ToolRAGStore) -> None:
    await register_control_tools(store)
    await register_analyst_tools(store)
    await register_orchestrator_tools(store)


def _make_step(n: int, status: str = "pending") -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role="core_dev",
        action="read_file",
        target_file=f"file_{n}.py",
        description="Stub step.",
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


# The 5 tools the orchestrator must be able to retrieve after Wave 3.
_WAVE3_TOOLS = [
    "get_wbs_status",
    "emit_hitl_request",
    "ask_user_question",
    "toggle_plan_mode",
    "read_token_ledger",
]

# The 2 net-new orchestrator-only tools.
_ORCHESTRATOR_ONLY = ["get_wbs_status", "emit_hitl_request"]


# =====================================================================
# A — Role surface + retrievability
# =====================================================================


@pytest.mark.anyio
async def test_all_wave3_tools_have_orchestrator_role(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _WAVE3_TOOLS:
        assert tool_name in schemas, f"Schema {tool_name!r} missing from store"
        assert "orchestrator" in schemas[tool_name].allowed_roles, (
            f"{tool_name!r} missing 'orchestrator' in allowed_roles"
        )


@pytest.mark.anyio
async def test_select_tools_surfaces_orchestrator_tools(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for tool_name in _WAVE3_TOOLS:
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="orchestrator",
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {s.name for s in results}
        assert tool_name in names, f"{tool_name!r} not surfaced for orchestrator"


# =====================================================================
# B — READ_ONLY + survive PLAN mode
# =====================================================================


@pytest.mark.anyio
async def test_all_orchestrator_tools_are_read_only(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _WAVE3_TOOLS:
        assert schemas[tool_name].privilege_tier == ToolPrivilegeTier.READ_ONLY, (
            f"{tool_name!r} expected READ_ONLY"
        )


@pytest.mark.anyio
async def test_orchestrator_tools_survive_plan_mode(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for tool_name in _WAVE3_TOOLS:
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="orchestrator",
            session_mode=SessionPermissionMode.PLAN,
        )
        names = {s.name for s in results}
        assert tool_name in names, f"{tool_name!r} not returned under PLAN mode"


# =====================================================================
# C — Negative RBAC + wire-in regression
# =====================================================================


@pytest.mark.anyio
async def test_non_orchestrator_roles_cannot_retrieve_net_new(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    for role in ("vcs_manager", "analyst"):
        results = await store.select_tools(
            "wbs status hitl approval gate",
            k=10,
            active_role=role,
            session_mode=SessionPermissionMode.DEFAULT,
        )
        names = {s.name for s in results}
        for tool_name in _ORCHESTRATOR_ONLY:
            assert tool_name not in names, (
                f"{role!r} should NOT see orchestrator-only tool {tool_name!r}"
            )


@pytest.mark.anyio
async def test_read_token_ledger_still_visible_to_analyst(tmp_path: Path) -> None:
    """The role union must not drop analyst from read_token_ledger."""
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    roles = schemas["read_token_ledger"].allowed_roles
    assert "analyst" in roles, "analyst regressed off read_token_ledger"
    assert "orchestrator" in roles, "orchestrator not added to read_token_ledger"


@pytest.mark.anyio
async def test_control_tools_keep_all_canonical_roles(tmp_path: Path) -> None:
    """ask_user_question / toggle_plan_mode keep the 8 roles AND gain orchestrator."""
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in ("ask_user_question", "toggle_plan_mode"):
        roles = schemas[tool_name].allowed_roles
        assert _CONTROL_ROLES <= roles, f"{tool_name!r} dropped a canonical role"
        assert "orchestrator" in roles, f"{tool_name!r} missing orchestrator"


# =====================================================================
# D — GetWBSStatusTool behaviour
# =====================================================================


@pytest.mark.anyio
async def test_get_wbs_status_aggregates(tmp_path: Path) -> None:
    tasks = [
        _make_step(1, status="completed"),
        _make_step(2, status="in_progress"),
        _make_step(3, status="pending"),
    ]
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    tool = GetWBSStatusTool(state=state)

    payload = json.loads(await tool._arun(include_steps=True))
    assert payload["status"] == "ok"
    assert payload["total"] == 3
    assert payload["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 1,
        "failed": 0,
    }
    assert payload["active_step"] == 2  # first non-terminal
    assert len(payload["tasks"]) == 3
    assert payload["truncated"] is False


@pytest.mark.anyio
async def test_get_wbs_status_include_steps_false(tmp_path: Path) -> None:
    state: Dict[str, Any] = {"mission_spec": _make_mission([_make_step(1)])}
    tool = GetWBSStatusTool(state=state)

    payload = json.loads(await tool._arun(include_steps=False))
    assert "tasks" not in payload
    assert payload["total"] == 1


@pytest.mark.anyio
async def test_get_wbs_status_no_mission(tmp_path: Path) -> None:
    tool = GetWBSStatusTool(state={})
    payload = json.loads(await tool._arun())
    assert payload["status"] == "no_mission"


@pytest.mark.anyio
async def test_get_wbs_status_caps_large_wbs(tmp_path: Path) -> None:
    tasks = [_make_step(n) for n in range(1, 251)]  # 250 steps
    state: Dict[str, Any] = {"mission_spec": _make_mission(tasks)}
    tool = GetWBSStatusTool(state=state)

    payload = json.loads(await tool._arun(include_steps=True))
    assert payload["total"] == 250
    assert len(payload["tasks"]) <= _WBS_MAX_STEPS
    assert payload["truncated"] is True


@pytest.mark.anyio
async def test_get_wbs_status_none_tasks_no_typeerror(tmp_path: Path) -> None:
    """A mission whose .tasks is None must degrade, not raise TypeError."""
    state: Dict[str, Any] = {"mission_spec": SimpleNamespace(tasks=None)}
    tool = GetWBSStatusTool(state=state)

    payload = json.loads(await tool._arun())
    assert payload["status"] == "no_mission"


# =====================================================================
# E — EmitHITLRequestTool behaviour
# =====================================================================


@pytest.mark.anyio
async def test_emit_hitl_request_emits_and_records(tmp_path: Path) -> None:
    state: Dict[str, Any] = {}
    tool = EmitHITLRequestTool(state=state)

    result = await tool._arun(target_role="devops_infra", trigger=".env")
    assert result.startswith("[emit_hitl_request] HITL_GATE:")

    channel = state["hitl_approval_requests"]
    assert len(channel) == 1
    assert channel[0]["flag"] == "HITL_APPROVAL_REQUIRED:devops_infra:.env"
    assert channel[0]["request_id"]


@pytest.mark.anyio
async def test_emit_hitl_request_idempotent_within_state(tmp_path: Path) -> None:
    state: Dict[str, Any] = {}
    tool = EmitHITLRequestTool(state=state)

    r1 = await tool._arun(target_role="devops_infra", trigger=".env")
    r2 = await tool._arun(target_role="devops_infra", trigger=".env")
    assert r1 == r2
    assert len(state["hitl_approval_requests"]) == 1  # no duplicate appended


@pytest.mark.anyio
async def test_emit_hitl_request_deterministic_across_fresh_state(tmp_path: Path) -> None:
    """Same gate -> same request_id even on a brand-new (dropped) state channel."""
    r1 = await EmitHITLRequestTool(state={})._arun(
        target_role="devops_infra", trigger=".env"
    )
    r2 = await EmitHITLRequestTool(state={})._arun(
        target_role="devops_infra", trigger=".env"
    )
    assert r1 == r2


@pytest.mark.anyio
async def test_emit_hitl_request_sanitizes_injection(tmp_path: Path) -> None:
    state: Dict[str, Any] = {}
    tool = EmitHITLRequestTool(state=state)

    await tool._arun(target_role="devops:infra\n", trigger="x:y")
    flag = state["hitl_approval_requests"][0]["flag"]
    # Exactly the 2 structural delimiters survive; injected colons/newline are replaced.
    assert flag.count(":") == 2
    segments = flag.split(":")
    assert segments == ["HITL_APPROVAL_REQUIRED", "devops_infra", "x_y"]


@pytest.mark.anyio
async def test_emit_hitl_request_empty_field_guard(tmp_path: Path) -> None:
    state: Dict[str, Any] = {}
    tool = EmitHITLRequestTool(state=state)

    payload = json.loads(await tool._arun(target_role="devops_infra", trigger="   "))
    assert "error" in payload
    assert "hitl_approval_requests" not in state  # nothing emitted


@pytest.mark.anyio
async def test_emit_hitl_request_truncates_reason(tmp_path: Path) -> None:
    state: Dict[str, Any] = {}
    tool = EmitHITLRequestTool(state=state)

    await tool._arun(
        target_role="devops_infra",
        trigger=".env",
        reason="line1\nline2 " + "x" * 500,
    )
    reason = state["hitl_approval_requests"][0]["reason"]
    assert reason is not None
    assert "\n" not in reason
    assert len(reason) <= 256


# =====================================================================
# F — read_token_ledger executes for orchestrator context (smoke)
# =====================================================================


@pytest.mark.anyio
async def test_token_ledger_tool_executes() -> None:
    from tools.analyst_tools import TokenLedgerReadTool

    ledger = TokenLedger()
    ledger.record_local(100, 50)
    tool = TokenLedgerReadTool(ledger=ledger)

    payload = json.loads(await tool._arun(tier="all"))
    assert payload["local_tokens"] == 150.0
