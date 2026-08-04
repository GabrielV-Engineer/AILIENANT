# tests/test_subagent_arsenal.py
"""DEBT-106 + DEBT-127 — dev-role tool arsenal + prompt overrides for dispatched
subagents.

Covers:
  C. `_resolve_tools` (brain/nodes/subagent_worker_node.py): a dev role resolves
     a non-empty, role-scoped arsenal through the same select_tools ->
     resolve_tools substrate the agentic cell and coder grounding pre-pass use;
     `analyst_readonly` still routes to its own dedicated `build_analyst_tools`
     builder (analyst tools register under `allowed_roles={"analyst"}`, disjoint
     from the `analyst_readonly` dispatch identity — the shared catalog path
     would return nothing for it); an unknown role stays tool-less; a
     resolution fault degrades without raising.
  D. `build_subagent_system_prompt` (agents/roles.py): a saved per-role
     override reaches both the tool-loop seed and the final-answer
     synthesiser; an unregistered role (e.g. analyst_readonly) falls back to
     the language-mirror directive rather than core_dev's; an injected
     `dispatch_answer_fn` keeps working unchanged (AnswerFn signature intact).
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from brain.nodes.subagent_worker_node import _resolve_tools, subagent_worker
from brain.subagent_contracts import SubagentResponseField, SubagentResponseSchema, SubagentTask
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


def _task(i: int, role: str = "core_dev") -> SubagentTask:
    return SubagentTask(
        task_id=f"t{i}",
        description=f"do work {i}",
        subagent_role=role,  # type: ignore[arg-type]
        response_schema=SubagentResponseSchema(
            fields=[SubagentResponseField(name="summary", type="str", description="one line")]
        ),
    )


async def _answer_fn(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    return {"summary": f"done {task.task_id}"}


# ── C. _resolve_tools ─────────────────────────────────────────────────────────


async def test_dev_role_resolves_nonempty_role_scoped_arsenal(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _isolated_store()
    await store.register_schema(
        ToolSchema(
            name="run_linter",
            description="Lint a file.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"qa_tester"}),
        )
    )
    # A decoy schema scoped to a DIFFERENT role must not leak into qa_tester's set.
    await store.register_schema(
        ToolSchema(
            name="run_data_pipeline",
            description="Run a data pipeline.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.EXECUTE,
            allowed_roles=frozenset({"data_ml_engineer"}),
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    tools = await _resolve_tools(
        "qa_tester", {}, intent="fix the failing tests", session_mode=SessionPermissionMode.STANDARD,
    )
    assert "run_linter" in tools
    assert "run_data_pipeline" not in tools


async def test_analyst_readonly_still_uses_dedicated_analyst_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """analyst_readonly must route to build_analyst_tools regardless of the
    catalog contents — analyst tools register under allowed_roles={"analyst"},
    disjoint from this dispatch identity, so the shared catalog path can never
    serve it."""
    store = _isolated_store()
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)  # empty catalog

    tools = await _resolve_tools(
        "analyst_readonly", {"workspace_root": "/ws", "project_id": "p1", "session_id": "s1"},
        intent="review this change", session_mode=SessionPermissionMode.READ_ONLY,
    )
    assert tools, "analyst_readonly must resolve the analyst arsenal even with an empty tool_rag catalog"
    assert "run_linter" in tools


async def test_unknown_role_stays_toolless(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _isolated_store()
    await store.register_schema(
        ToolSchema(
            name="run_linter",
            description="Lint a file.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"some_future_role"}),
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    tools = await _resolve_tools(
        "some_future_role", {}, intent="anything", session_mode=SessionPermissionMode.STANDARD,
    )
    assert tools == {}


async def test_dev_role_resolution_fault_degrades_to_toolless(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomStore:
        async def select_tools(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr("core.tool_rag.tool_rag_store", _BoomStore())

    tools = await _resolve_tools(
        "core_dev", {}, intent="anything", session_mode=SessionPermissionMode.STANDARD,
    )
    assert tools == {}


# ── D. build_subagent_system_prompt + override threading ────────────────────


def test_build_subagent_system_prompt_uses_role_directive() -> None:
    from agents.roles import build_subagent_system_prompt

    prompt = build_subagent_system_prompt("qa_tester")
    assert "qa_tester" in prompt
    assert "LANGUAGE" in prompt
    # The coder's SEARCH/REPLACE contract must NOT leak into the subagent prompt —
    # a response_schema-constrained JSON answer is the wrong contract for it.
    assert "SEARCH" not in prompt


def test_build_subagent_system_prompt_override_replaces_directive() -> None:
    from agents.roles import build_subagent_system_prompt

    prompt = build_subagent_system_prompt("qa_tester", override="Only write snapshot tests.")
    assert "Only write snapshot tests." in prompt
    assert "Write tests using the project's existing test" not in prompt


def test_build_subagent_system_prompt_unregistered_role_falls_back_to_language_only() -> None:
    from agents.roles import build_subagent_system_prompt

    prompt = build_subagent_system_prompt("analyst_readonly")
    assert "LANGUAGE" in prompt
    # Must NOT silently inherit core_dev's directive (get_role_config's fallback).
    assert "business logic" not in prompt


async def test_role_override_reaches_tool_loop_seed_and_answer_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    await store.register_schema(
        ToolSchema(
            name="run_linter",
            description="Lint a file.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"qa_tester"}),
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    captured_loop: Dict[str, Any] = {}

    async def _stop_reasoner(messages: Any) -> str:
        captured_loop["messages"] = list(messages)
        return "{}"

    captured_answer: Dict[str, Any] = {}

    async def _fake_acomplete(*, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        captured_answer["messages"] = messages
        return '{"summary": "ok"}'

    with patch(
        "tools.llm_gateway.LLMGateway.acomplete_with_thinking",
        new=AsyncMock(side_effect=_fake_acomplete),
    ):
        result = await subagent_worker(
            {
                "_dispatch_task": _task(0, role="qa_tester").model_dump(),
                "session_permission_mode": "READ_ONLY",
                "agent_role_overrides": {"qa_tester": "Custom qa directive text."},
            },
            {"configurable": {"dispatch_tool_reasoner": _stop_reasoner}},
        )

    loop_system_msgs = [
        m["content"] for m in captured_loop.get("messages", []) if m.get("role") == "system"
    ]
    assert any("Custom qa directive text." in s for s in loop_system_msgs), loop_system_msgs

    answer_system_msgs = [
        m["content"] for m in captured_answer.get("messages", []) if m.get("role") == "system"
    ]
    assert any("Custom qa directive text." in s for s in answer_system_msgs), answer_system_msgs

    assert result["_dispatch_results"][0]["status"] == "ok"


async def test_injected_answer_fn_keeps_working_unchanged() -> None:
    """The AnswerFn signature is untouched by the override threading — an
    injected dispatch_answer_fn (the hermetic-test seam) still runs unmodified."""
    result = await subagent_worker(
        {"_dispatch_task": _task(0).model_dump(), "session_permission_mode": "READ_ONLY"},
        {"configurable": {"dispatch_answer_fn": _answer_fn}},
    )
    env = result["_dispatch_results"][0]
    assert env["status"] == "ok"
    assert env["structured_result"] == {"summary": "done t0"}
