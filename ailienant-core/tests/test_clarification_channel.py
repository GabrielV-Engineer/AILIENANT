# tests/test_clarification_channel.py
"""DEBT-171/172 — defer-then-interrupt-first for ask_user_question.

ask_user_question is READ_ONLY, so it dispatches straight through the agentic
cell's registry-fallback loop (classify() never resolves it to HITL) — but
interrupt() cannot run safely mid-dispatch-loop, so the tool's own write into
state['pending_hitl_request'] is what the loop detects to defer, mirroring the
existing pending_tool_call (DEBT-129) two-phase pattern. Six things are tested:

  1. Defer sets pending_hitl_request and halts the loop — no further dispatch.
  2. A preceding sibling's edit survives the defer; a following sibling never runs.
  3. The resume phase calls the clarification seam first and folds the answer in.
  4. The coder's READ_ONLY grounding pre-pass never offers the tool.
  5. The outbound HITL card carries `question` (and falls back into
     `action_description`), correlated via `request_id` (clarification payloads
     have no `approval_id`).
  6. The inbound resume dict carries `answer`/`selected_option` and falls back
     `answer` to `comment`.

DEBT-172 additionally covers the multi-question batch extension: the outbound
card synthesizes an action_description when only `questions` is present, the
resume phase folds a batch `answers` result into one readable trajectory line
per question, and the inbound resume dict forwards `answers` verbatim.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

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

pytestmark = pytest.mark.anyio


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


async def _register(store: ToolRAGStore, name: str) -> None:
    await store.register_schema(
        ToolSchema(
            name=name,
            description=f"decoy {name}",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"core_dev"}),
        )
    )


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# ── 1 & 2 — defer halts the loop, preserves preceding-sibling state ──────────


def test_ask_user_question_defers_and_halts_further_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    asyncio.run(_register(store, "ask_user_question"))
    asyncio.run(_register(store, "todo_write"))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[
        ToolCall("ask_user_question", {
            "question": "Which approach?", "suggested_options": ["A", "B"],
        }),
        ToolCall("todo_write", {
            "todos": [{"content": "x", "status": "pending", "active_form": "x"}]
        }),
    ]])
    state = _base_state()

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    pending = delta.get("pending_hitl_request")
    assert pending is not None
    assert pending["kind"] == "ASK_USER_QUESTION"
    assert pending["question"] == "Which approach?"
    assert pending["suggested_options"] == ["A", "B"]
    assert delta["agentic_trajectory"][0]["status"] == "continue"
    # The sentinel HITL_PENDING string never reached the trajectory.
    assert not any(
        "HITL_PENDING" in m.get("content", "")
        for m in delta["agentic_trajectory"] if isinstance(m, dict)
    )
    # todo_write — the sibling call AFTER ask_user_question — never ran.
    assert "agent_todos" not in delta
    assert session.run_calls == []


def test_preceding_sibling_edit_survives_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preceding apply_granular_edit's mutation rides out on the defer delta
    unchanged; the following todo_write call never executes."""
    store = _isolated_store()
    asyncio.run(_register(store, "ask_user_question"))
    asyncio.run(_register(store, "todo_write"))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    def fake_apply(
        read: Any, write: Any, path: str, search: str, replace: str,
        expected_hash: Any = None,
    ) -> str:
        write(path, replace)
        return "diff"

    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    reasoner = _reasoner_from([[
        ToolCall("apply_granular_edit", {"path": "a.py", "search": "x = 1", "replace": "x = 2"}),
        ToolCall("ask_user_question", {"question": "Which approach?"}),
        ToolCall("todo_write", {
            "todos": [{"content": "x", "status": "pending", "active_form": "x"}]
        }),
    ]])
    state = _base_state()

    with patch("tools.patch_tool.apply_patch_to_vfs", side_effect=fake_apply):
        delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert delta["pending_contents"]["a.py"] == "x = 2"
    assert delta.get("pending_hitl_request") is not None
    assert "agent_todos" not in delta


# ── 3 — resume phase interrupts first, folds the answer into the trajectory ──


async def test_resume_phase_calls_clarification_seam_first_and_folds_answer() -> None:
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    clarify = AsyncMock(return_value={"answer": "Use approach A", "selected_option": None})

    async def _never_called(_messages: Any) -> List[ToolCall]:
        raise AssertionError("reasoner must not run during the clarification-resume phase")

    state = _base_state(
        pending_hitl_request={
            "request_id": "abc123",
            "kind": "ASK_USER_QUESTION",
            "question": "Which approach?",
            "context": None,
            "suggested_options": ["A", "B"],
            "requested_at": "2026-01-01T00:00:00Z",
        },
    )
    config = _config(adapter, _never_called, cell_clarification_fn=clarify)

    delta = await run_agentic_cell_node(state, config)

    clarify.assert_awaited_once()
    assert clarify.call_args.args[0]["question"] == "Which approach?"
    assert delta.get("pending_hitl_request") is None
    trajectory = delta["agentic_trajectory"]
    obs = [m for m in trajectory if isinstance(m, dict) and m.get("role") == "system"]
    assert any("Use approach A" in m["content"] for m in obs)
    assert any("Which approach?" in m["content"] for m in obs)


async def test_resume_phase_falls_back_when_operator_gives_no_answer() -> None:
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    clarify = AsyncMock(return_value={"answer": None, "selected_option": None})

    async def _never_called(_messages: Any) -> List[ToolCall]:
        raise AssertionError("reasoner must not run during the clarification-resume phase")

    state = _base_state(
        pending_hitl_request={
            "request_id": "abc123", "kind": "ASK_USER_QUESTION",
            "question": "Which approach?", "context": None,
            "suggested_options": [], "requested_at": "2026-01-01T00:00:00Z",
        },
    )
    config = _config(adapter, _never_called, cell_clarification_fn=clarify)

    delta = await run_agentic_cell_node(state, config)

    trajectory = delta["agentic_trajectory"]
    obs = [m for m in trajectory if isinstance(m, dict) and m.get("role") == "system"]
    assert any("no answer" in m["content"] for m in obs)


async def test_resume_phase_folds_multi_question_answers_into_trajectory() -> None:
    """DEBT-172 — a batch resume result (`answers`) folds into one readable
    trajectory line per question, id-correlated back to its header."""
    session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
    adapter = StubAdapter(session, StubSyncSurface())
    clarify = AsyncMock(return_value={
        "answer": None, "selected_option": None,
        "answers": [
            {"id": "q0", "selected_labels": ["Single container"], "free_text": None},
            {"id": "q1", "selected_labels": [], "free_text": "Later this week"},
        ],
    })

    async def _never_called(_messages: Any) -> List[ToolCall]:
        raise AssertionError("reasoner must not run during the clarification-resume phase")

    state = _base_state(
        pending_hitl_request={
            "request_id": "abc123",
            "kind": "ASK_USER_QUESTION",
            "questions": [
                {"id": "q0", "header": "Docker setup", "question": "How to dockerize?",
                 "context": None, "options": [], "multi_select": False},
                {"id": "q1", "header": "Docs", "question": "Commit docs now?",
                 "context": None, "options": [], "multi_select": False},
            ],
            "requested_at": "2026-01-01T00:00:00Z",
        },
    )
    config = _config(adapter, _never_called, cell_clarification_fn=clarify)

    delta = await run_agentic_cell_node(state, config)

    assert delta.get("pending_hitl_request") is None
    trajectory = delta["agentic_trajectory"]
    obs = [m for m in trajectory if isinstance(m, dict) and m.get("role") == "system"]
    combined = " ".join(m["content"] for m in obs)
    assert "Docker setup: Single container" in combined
    assert "Docs: Later this week" in combined


# ── 4 — the coder's READ_ONLY grounding pre-pass never offers the tool ───────


async def test_grounding_loop_excludes_ask_user_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain.state import WBSStep
    from core.tool_rag import tool_rag_store
    import core.tool_registry as tool_registry_module

    schemas = [
        ToolSchema(
            name="ask_user_question", description="ask", json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
        ),
        ToolSchema(
            name="read_symbol_source", description="read", json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"core_dev"}),
        ),
    ]

    async def _fake_select_tools(*_a: Any, **_k: Any) -> List[ToolSchema]:
        return schemas

    captured: Dict[str, List[str]] = {}

    def _fake_resolve_tools(passed_schemas: Any, _state: Any) -> List[Any]:
        captured["names"] = [s.name for s in passed_schemas]
        return []  # short-circuits _run_grounding_loop to "" before any LLM call

    monkeypatch.setattr(tool_rag_store, "select_tools", _fake_select_tools)
    monkeypatch.setattr(tool_registry_module, "resolve_tools", _fake_resolve_tools)

    step = WBSStep(
        step_number=1, action="edit_file", target_file="a.py",
        description="do it", target_role="core_dev",
    )
    from agents.coder import _run_grounding_loop

    result = await _run_grounding_loop(step, {"session_permission_mode": "AUTO"}, "sess-1")

    assert result == ""
    assert captured["names"] == ["read_symbol_source"]


# ── 5 — outbound card carries the question, correlated via request_id ────────


async def test_emit_interrupt_card_surfaces_question_and_falls_back_correlation_id() -> None:
    from core.task_service import TaskService

    ts = TaskService()  # type: ignore[no-untyped-call]
    sent: List[Any] = []

    async def _capture(session_id: str, event: Any) -> None:
        sent.append(event)

    with patch("core.task_service.vfs_manager.send_personal_message", new=_capture):
        await ts._emit_interrupt_card(
            "sess-1",
            {
                "session_id": "sess-1",
                "request_id": "req-xyz",  # clarification shape — no approval_id at all
                "request_kind": "CLARIFICATION_NEEDED",
                "question": "Which approach?",
                "context": "some context",
                "suggested_options": ["A", "B"],
            },
        )

    assert len(sent) == 1
    data = sent[0].data
    # request_id backs the correlation id — a clarification has no approval_id.
    assert data.approval_id == "req-xyz"
    # question backs action_description so today's plain card already renders it.
    assert data.action_description == "Which approach?"
    assert data.question == "Which approach?"
    assert data.context == "some context"
    assert data.suggested_options == ["A", "B"]


async def test_emit_interrupt_card_surfaces_questions_batch_with_synthesized_description() -> None:
    """DEBT-172 — a pure batch payload (no legacy `question`) still gets a
    sensible action_description fallback, and `questions` rides through."""
    from core.task_service import TaskService

    ts = TaskService()  # type: ignore[no-untyped-call]
    sent: List[Any] = []

    async def _capture(session_id: str, event: Any) -> None:
        sent.append(event)

    batch = [
        {
            "id": "q0", "header": "Docker setup", "question": "How to dockerize?",
            "context": None,
            "options": [{"label": "Single container", "description": None, "recommended": True}],
            "multi_select": False,
        },
    ]
    with patch("core.task_service.vfs_manager.send_personal_message", new=_capture):
        await ts._emit_interrupt_card(
            "sess-1",
            {
                "session_id": "sess-1",
                "request_id": "req-batch",
                "request_kind": "CLARIFICATION_NEEDED",
                "questions": batch,
            },
        )

    data = sent[0].data
    assert data.approval_id == "req-batch"
    assert data.action_description == "I have 1 question(s) before continuing."
    assert data.question is None
    assert data.questions is not None
    assert [q.model_dump() for q in data.questions] == batch


async def test_emit_interrupt_card_approval_shape_is_unaffected() -> None:
    from core.task_service import TaskService

    ts = TaskService()  # type: ignore[no-untyped-call]
    sent: List[Any] = []

    async def _capture(session_id: str, event: Any) -> None:
        sent.append(event)

    with patch("core.task_service.vfs_manager.send_personal_message", new=_capture):
        await ts._emit_interrupt_card(
            "sess-1",
            {
                "session_id": "sess-1",
                "approval_id": "appr-1",
                "action_description": "COMMAND_EXEC: rm -rf /tmp/x",
                "request_kind": "COMMAND_EXEC",
            },
        )

    data = sent[0].data
    assert data.approval_id == "appr-1"
    assert data.action_description == "COMMAND_EXEC: rm -rf /tmp/x"
    assert data.question is None


# ── 6 — inbound resume dict carries answer/selected_option, falls back to comment ─


def test_resume_approval_dict_forwards_explicit_answer() -> None:
    from api.ws_contracts import HITLResponsePayload
    from main import _resume_approval_dict

    data = HITLResponsePayload(
        approval_id="req-xyz", approved=True,
        comment="ignored", answer="Use approach A", selected_option="A",
    )
    result = _resume_approval_dict(data)
    assert result == {
        "approved": True, "comment": "ignored",
        "answer": "Use approach A", "selected_option": "A", "answers": None,
    }


def test_resume_approval_dict_falls_back_answer_to_comment() -> None:
    """Today's frontend has no multi-choice renderer (DEBT-172) — it always sends
    answer=None, so the operator's free-text comment must still reach the graph."""
    from api.ws_contracts import HITLResponsePayload
    from main import _resume_approval_dict

    data = HITLResponsePayload(approval_id="req-xyz", approved=True, comment="Use approach A")
    result = _resume_approval_dict(data)
    assert result["answer"] == "Use approach A"
    assert result["selected_option"] is None


def test_resume_approval_dict_forwards_answers_batch() -> None:
    from api.ws_contracts import ClarificationAnswer, HITLResponsePayload
    from main import _resume_approval_dict

    data = HITLResponsePayload(
        approval_id="req-xyz", approved=True,
        answers=[ClarificationAnswer(id="q0", selected_labels=["Single container"])],
    )
    result = _resume_approval_dict(data)
    assert result["answers"] == [
        {"id": "q0", "selected_labels": ["Single container"], "free_text": None}
    ]
