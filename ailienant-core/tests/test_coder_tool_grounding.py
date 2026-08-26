# tests/test_coder_tool_grounding.py
"""DEBT-130 — READ_ONLY tool-grounding pre-pass for the one-shot coder path.

Covers:
  A. `_needs_grounding` heuristic — skips when already grounded (file read +
     RAG snippet + no retry), fires on new-file / empty-RAG / retry.
  B. `_grounding_admitted` — READ_ONLY resolves ALLOW under ordinary modes,
     HITL under ASK_ALL (this pre-pass never wires an approval channel).
  C. End-to-end: the pre-pass is skipped (zero extra LLM call) when not
     needed; fires and feeds observations into the assembled prompt when
     needed; a WRITE-tier schema is filtered out before the loop ever sees
     it; a failure anywhere in the loop degrades to the pre-existing
     (no tool-calling) behavior without touching the SEARCH/REPLACE contract.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep
from core.permissions import SessionPermissionMode
from core.tool_rag import ToolRAGStore, ToolSchema
from core.permissions import ToolPrivilegeTier


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _fake_llm_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _make_step(
    role: str = "core_dev",
    action: str = "write_file",
    target_file: str = "new_module.py",
    description: str = "Create the new module.",
) -> WBSStep:
    return WBSStep(
        step_number=1,
        target_role=role,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        target_file=target_file,
        description=description,
        status="pending",
    )


def _make_state(step: WBSStep, **overrides: Any) -> Dict[str, Any]:
    mission = MissionSpecification(
        outcome="o", scope=[step.target_file], constraints=["c"],
        decisions=["d"], tasks=[step], checks=["k"],
    )
    state: Dict[str, Any] = {
        "task_id": "grounding-test",
        "mission_spec": mission,
        "current_step_id": 1,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }
    state.update(overrides)
    return state


async def _fake_embed(text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


# ── A. _needs_grounding heuristic (pure) ─────────────────────────────────────


def test_needs_grounding_skips_when_already_grounded() -> None:
    from agents.coder import _needs_grounding

    step = _make_step()
    assert _needs_grounding(
        step, "def foo(): pass", [("other.py", "def bar(): pass")], {}
    ) is False


def test_needs_grounding_fires_on_new_file() -> None:
    from agents.coder import _needs_grounding

    step = _make_step()
    assert _needs_grounding(step, None, [("other.py", "def bar(): pass")], {}) is True


def test_needs_grounding_fires_on_empty_rag() -> None:
    from agents.coder import _needs_grounding

    step = _make_step()
    assert _needs_grounding(step, "def foo(): pass", [], {}) is True
    # A snippet list whose entries are all empty text is equally "nothing usable".
    assert _needs_grounding(step, "def foo(): pass", [("other.py", "")], {}) is True


def test_needs_grounding_fires_on_retry_after_validation_feedback() -> None:
    from agents.coder import _needs_grounding

    step = _make_step()
    state = {"validation_feedback": "SEARCH block did not match."}
    assert _needs_grounding(
        step, "def foo(): pass", [("other.py", "def bar(): pass")], state
    ) is True


# ── B. _grounding_admitted (pure) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode",
    [
        SessionPermissionMode.FULL_AUTO,
        SessionPermissionMode.STANDARD,
        SessionPermissionMode.CAUTIOUS,
        SessionPermissionMode.ASK_EXECUTE,
        SessionPermissionMode.READ_ONLY,
        SessionPermissionMode.PLAN_ONLY,
    ],
)
def test_grounding_admitted_true_for_ordinary_modes(mode: SessionPermissionMode) -> None:
    from agents.coder import _grounding_admitted

    assert _grounding_admitted(mode) is True


def test_grounding_admitted_false_under_ask_all() -> None:
    """ASK_ALL resolves READ_ONLY to HITL, and this pre-pass never wires an
    approval channel — admitting here would burn a full reasoning round-trip
    only to have every candidate call denied."""
    from agents.coder import _grounding_admitted

    assert _grounding_admitted(SessionPermissionMode.ASK_ALL) is False


# ── C. End-to-end via run_coder_node ─────────────────────────────────────────


async def _run_with_mocks(
    state: Dict[str, Any],
    *,
    file_content: Any,
    rag_snippets: List[Any],
    ainvoke_responses: List[Any],
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Drive run_coder_node with I/O sealed; return (result, captured ainvoke calls).

    Both the grounding pre-pass's gateway reasoner and the main SEARCH/REPLACE
    generation call route through LLMGateway.ainvoke (acomplete_with_thinking's
    fallback branch delegates to it when no reasoning sink is wired — the
    default in these tests), so one mock, keyed by call order, covers both.
    """
    from core.vfs_middleware import VFSReadResult

    calls: List[Dict[str, Any]] = []

    async def _fake_ainvoke(*, messages: List[Dict[str, str]], **kwargs: Any) -> Any:
        calls.append({"messages": messages, **kwargs})
        idx = min(len(calls) - 1, len(ainvoke_responses) - 1)
        return ainvoke_responses[idx]

    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
        new=AsyncMock(return_value=rag_snippets),
    ), patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=file_content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=_fake_ainvoke),
    ):
        from agents.coder import run_coder_node

        result = await run_coder_node(state)
    return result, calls


async def test_grounding_skipped_pays_zero_extra_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-grounded step (file exists + RAG hit + no retry) → the pre-pass
    never fires, so exactly ONE ainvoke call happens (the generation itself)."""
    monkeypatch.setattr("core.tool_rag.tool_rag_store", _isolated_store())
    step = _make_step(action="edit_file", target_file="existing.py")
    state = _make_state(step)

    _result, calls = await _run_with_mocks(
        state,
        file_content="def foo():\n    return 1\n",
        rag_snippets=[("other.py", "def bar(): pass")],
        ainvoke_responses=[_fake_llm_response("")],
    )
    assert len(calls) == 1, f"expected exactly 1 ainvoke call, got {len(calls)}"


async def test_grounding_fires_on_new_file_and_feeds_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New file + empty RAG → the pre-pass fires, calls the registered READ_ONLY
    tool, and its observation reaches the instruction sent to the generation
    call — while the SEARCH/REPLACE output contract stays untouched."""
    store = _isolated_store()
    await store.register_schema(
        ToolSchema(
            name="todo_write",
            description="Write your task TODO list to shared state.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"core_dev"}),
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    step = _make_step(action="write_file", target_file="brand_new_fires.py")
    state = _make_state(step)

    tool_call_envelope = (
        '{"tool_calls":[{"name":"todo_write","args":{"todos":[]}}]}'
    )
    stop_envelope = "{}"
    generation_output = ""  # no edits needed for this assertion

    result, calls = await _run_with_mocks(
        state,
        file_content=None,  # new file
        rag_snippets=[],
        ainvoke_responses=[
            _fake_llm_response(tool_call_envelope),
            _fake_llm_response(stop_envelope),
            _fake_llm_response(generation_output),
        ],
    )

    # At least 2 calls happened before the final generation call (grounding loop).
    assert len(calls) >= 2
    final_call = calls[-1]
    final_messages = final_call["messages"]
    user_content = "\n".join(
        m["content"] for m in final_messages if m.get("role") == "user"
    )
    assert "Tool-grounding observations" in user_content
    assert "todo_write" in user_content
    # The SEARCH/REPLACE format contract is untouched — same postamble text.
    assert "SEARCH/REPLACE edit blocks" in user_content
    # The coder's own step-completion contract still holds.
    assert "pending_patches" in result


async def test_grounding_filters_out_write_tier_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WRITE-tier schema is filtered out before the loop ever sees it — the
    grounding pass is a READ_ONLY ceiling regardless of what the catalog
    returns for this role/intent."""
    store = _isolated_store()
    await store.register_schema(
        ToolSchema(
            name="todo_write",
            description="Write your task TODO list to shared state.",
            json_schema="{}",
            privilege_tier=ToolPrivilegeTier.WRITE,  # deliberately non-READ_ONLY
            allowed_roles=frozenset({"core_dev"}),
        )
    )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    step = _make_step(action="write_file", target_file="brand_new_write_tier.py")
    state = _make_state(step)

    _result, calls = await _run_with_mocks(
        state,
        file_content=None,
        rag_snippets=[],
        ainvoke_responses=[_fake_llm_response("")],
    )
    # No READ_ONLY survivor → _run_grounding_loop returns "" before ever building
    # a dispatcher/reasoner, so only the generation call happens.
    assert len(calls) == 1


async def test_grounding_loop_failure_degrades_to_pre_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault anywhere in the grounding pre-pass (here: select_tools raising)
    must never block generation — it degrades to an empty grounding block,
    identical to the path before DEBT-130."""

    class _BoomStore:
        async def select_tools(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr("core.tool_rag.tool_rag_store", _BoomStore())

    step = _make_step(action="write_file", target_file="brand_new_boom.py")
    state = _make_state(step)

    result, calls = await _run_with_mocks(
        state,
        file_content=None,
        rag_snippets=[],
        ainvoke_responses=[_fake_llm_response("")],
    )
    assert len(calls) == 1, "the fault must be swallowed before any reasoner call"
    assert "pending_patches" in result
    assert result.get("errors", []) == [] or all(
        "grounding" not in e.lower() for e in result.get("errors", [])
    )
