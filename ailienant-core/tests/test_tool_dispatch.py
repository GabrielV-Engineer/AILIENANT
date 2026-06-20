"""Runtime tool-dispatch substrate + Analyst activation proof + coder skill injection.

Covers four contracts:
  1. Substrate units — envelope parsing and the gate-and-execute dispatch decision
     (unknown tool, role mismatch, permission DENY, READ_ONLY execute, tool raising).
  2. Self-correction — a reasoner that emits malformed JSON recovers via the feedback
     turn instead of crashing the loop.
  3. Analyst integration — run_analyst_node drives the loop, records an executed tool
     on tool_dispatch_trace, and still streams + suspends its Socratic question.
  4. Coder skill injection — an active skill is wrapped into the coder system prompt.

The model is sealed at the reasoner / stream boundary (the Gateway pattern); no live LLM.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Dict, List, Sequence
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.tools import BaseTool

from core.permissions import (
    SessionPermissionMode,
    ToolPrivilegeTier,
)
from core.tool_dispatch import (
    DispatchResult,
    RegisteredTool,
    ToolCall,
    ToolDispatcher,
    parse_tool_call_envelope,
)
from shared.rbac import PermissionMode

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Test doubles ──────────────────────────────────────────────────────────────


class _EchoTool(BaseTool):
    # args_schema intentionally omitted: the dispatcher invokes _arun directly, so
    # no schema override is needed (and none is asserted by the substrate).
    name: str = "echo"
    description: str = "Echo the value back."

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _arun(self, value: str) -> str:
        return f"echo:{value}"


class _BoomTool(BaseTool):
    name: str = "boom"
    description: str = "Always raises."

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _arun(self, value: str) -> str:
        raise RuntimeError("kaboom")


def _reg(tool: BaseTool, tier: ToolPrivilegeTier, roles: set[str]) -> RegisteredTool:
    return RegisteredTool(tool=tool, tier=tier, allowed_roles=frozenset(roles))


def _dispatcher(
    tools: Dict[str, RegisteredTool],
    *,
    active_role: str = "analyst",
    session_mode: SessionPermissionMode = SessionPermissionMode.DEFAULT,
    agent_permission: PermissionMode = PermissionMode.READ_ONLY,
) -> ToolDispatcher:
    return ToolDispatcher(
        tools,
        active_role=active_role,
        session_mode=session_mode,
        state={},
        agent_permission=agent_permission,
    )


def _scripted_reasoner(scripts: Sequence[str]) -> Callable[..., Any]:
    """A reasoner that returns each scripted reply in turn, then '{}' (stop)."""
    it = iter(scripts)

    async def _reason(messages: Sequence[Dict[str, Any]]) -> str:
        try:
            return next(it)
        except StopIteration:
            return "{}"

    return _reason


# ── 1. Envelope parsing ─────────────────────────────────────────────────────


def test_parse_single_call() -> None:
    calls, err = parse_tool_call_envelope('{"tool_calls":[{"name":"echo","args":{"value":"hi"}}]}')
    assert err is None
    assert calls == [ToolCall(name="echo", args={"value": "hi"})]


def test_parse_multi_call() -> None:
    calls, err = parse_tool_call_envelope(
        '{"tool_calls":[{"name":"a","args":{}},{"name":"b","args":{"x":1}}]}'
    )
    assert err is None
    assert [c.name for c in calls] == ["a", "b"]


def test_parse_empty_envelope_is_valid_stop() -> None:
    calls, err = parse_tool_call_envelope("{}")
    assert err is None
    assert calls == []


def test_parse_prose_wrapped_envelope() -> None:
    calls, err = parse_tool_call_envelope(
        'Sure! {"tool_calls":[{"name":"echo","args":{"value":"x"}}]} done.'
    )
    assert err is None
    assert calls and calls[0].name == "echo"


def test_parse_malformed_returns_error() -> None:
    calls, err = parse_tool_call_envelope("this is not json at all")
    assert calls == []
    assert err is not None


def test_parse_broken_json_returns_error() -> None:
    calls, err = parse_tool_call_envelope('{"tool_calls": [ {"name": }')
    assert calls == []
    assert err is not None


# ── 2. Dispatch gate decisions ───────────────────────────────────────────────


async def test_dispatch_unknown_tool_is_feedback_not_crash() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    result = await d.dispatch(ToolCall(name="ghost", args={}))
    assert isinstance(result, DispatchResult)
    assert result.executed is False
    assert "not found" in result.observation


async def test_dispatch_role_mismatch_denies() -> None:
    d = _dispatcher(
        {"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"coder"})},
        active_role="analyst",
    )
    result = await d.dispatch(ToolCall(name="echo", args={"value": "hi"}))
    assert result.executed is False
    assert "DENIED" in result.observation


async def test_dispatch_read_only_executes() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    result = await d.dispatch(ToolCall(name="echo", args={"value": "hi"}))
    assert result.executed is True
    assert result.observation == "echo:hi"


async def test_dispatch_write_tier_denied_by_gate() -> None:
    # Role is allowed, but a WRITE tier under a READ_ONLY agent identity is denied
    # by the permission matrix — proving the gate is actually consulted.
    d = _dispatcher(
        {"echo": _reg(_EchoTool(), ToolPrivilegeTier.WRITE, {"analyst"})},
        agent_permission=PermissionMode.READ_ONLY,
    )
    result = await d.dispatch(ToolCall(name="echo", args={"value": "hi"}))
    assert result.executed is False
    assert "not permitted" in result.observation


async def test_dispatch_tool_exception_is_caught() -> None:
    d = _dispatcher({"boom": _reg(_BoomTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    result = await d.dispatch(ToolCall(name="boom", args={"value": "x"}))
    assert result.executed is False
    assert "failed" in result.observation


async def test_dispatch_bad_args_is_caught() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    # Missing required 'value' kwarg → TypeError, surfaced as recoverable feedback.
    result = await d.dispatch(ToolCall(name="echo", args={"wrong": "x"}))
    assert result.executed is False
    assert "argument error" in result.observation


# ── 3. Self-correcting loop ──────────────────────────────────────────────────


async def test_run_loop_self_corrects_after_bad_json() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    reasoner = _scripted_reasoner(
        [
            "garbage not json",  # iter 1: malformed → feedback, no crash
            '{"tool_calls":[{"name":"echo","args":{"value":"ok"}}]}',  # iter 2: valid
        ]
    )
    messages: List[Dict[str, Any]] = []
    trace: List[ToolCall] = []
    await d.run_loop(messages, reasoner, max_iters=5, trace=trace)

    assert [c.name for c in trace] == ["echo"]
    feedback = [m for m in messages if "[dispatch]" in str(m.get("content", ""))]
    assert feedback, "malformed-JSON feedback turn must be injected for self-correction"


async def test_run_loop_bounded_on_persistent_bad_json() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})

    async def _always_bad(messages: Sequence[Dict[str, Any]]) -> str:
        return "never valid"

    messages: List[Dict[str, Any]] = []
    trace: List[ToolCall] = []
    await d.run_loop(messages, _always_bad, max_iters=3, trace=trace)
    assert trace == []
    # Exactly max_iters corrective turns — the loop terminates, no infinite spin.
    assert len(messages) == 3


async def test_run_loop_stops_on_empty_envelope() -> None:
    d = _dispatcher({"echo": _reg(_EchoTool(), ToolPrivilegeTier.READ_ONLY, {"analyst"})})
    reasoner = _scripted_reasoner(["{}"])
    messages: List[Dict[str, Any]] = []
    trace: List[ToolCall] = []
    await d.run_loop(messages, reasoner, max_iters=5, trace=trace)
    assert trace == []
    assert messages == []


# ── 4. Analyst node integration (DoD) ────────────────────────────────────────


async def test_analyst_node_invokes_tool_and_still_suspends(tmp_path: Any) -> None:
    target = tmp_path / "main.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    state: Dict[str, Any] = {
        "task_id": "analyst-dispatch-test",
        "user_input": "Help me refactor this module.",
        "messages": [],
        "errors": [],
        "security_flags": [],
        "hitl_pending": False,
        "shared_understanding_reached": False,
        "workspace_root": str(tmp_path),
        "active_file_path": str(target),
        "session_permission_mode": "DEFAULT",
    }

    # Injected reasoner: ask for one READ_ONLY diagnostic, then stop.
    reasoner = _scripted_reasoner(
        ['{"tool_calls":[{"name":"diff_changes","args":{"file_path":"%s"}}]}'
         % str(target).replace("\\", "\\\\")]
    )
    config: Any = {"configurable": {"analyst_tool_reasoner": reasoner}}

    async def _fake_question_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "What is the desired public API? Recommended: keep foo() signature."

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value="PERSONA"
    ), patch(
        "agents.analyst._stream_question_llm", new=_fake_question_stream
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_token",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_stream_end",
        new=AsyncMock(return_value=None),
    ):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(state, config)

    # DoD: a registered tool was invoked end-to-end through the gated substrate.
    trace = result.get("tool_dispatch_trace", [])
    assert trace, "expected a non-empty tool_dispatch_trace"
    assert trace[0]["name"] == "diff_changes"

    # The Socratic contract is preserved: the node still asks + suspends.
    assert result["hitl_pending"] is True
    assert result["shared_understanding_reached"] is False
    assert any(m.get("role") == "assistant" for m in result["messages"])


async def test_analyst_node_skips_loop_without_workspace() -> None:
    """No workspace to inspect → the loop is skipped and no trace key is emitted."""
    state: Dict[str, Any] = {
        "task_id": "analyst-no-ws",
        "user_input": "General question.",
        "messages": [],
        "errors": [],
        "security_flags": [],
        "hitl_pending": False,
        "shared_understanding_reached": False,
    }

    async def _fake_question_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield "What is the goal? Recommended: a working feature."

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value="PERSONA"
    ), patch(
        "agents.analyst._stream_question_llm", new=_fake_question_stream
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_token",
        new=AsyncMock(return_value=None),
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_stream_end",
        new=AsyncMock(return_value=None),
    ):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(state)

    assert "tool_dispatch_trace" not in result
    assert result["hitl_pending"] is True


# ── 5. Coder skill injection (DEBT-032) ──────────────────────────────────────


def _fake_llm_response(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


async def _capture_coder_system_prompt(state_overrides: Dict[str, Any]) -> str:
    """Run run_coder_node with I/O sealed, capturing the system message sent to the LLM."""
    from brain.state import MissionSpecification, WBSStep
    from core.vfs_middleware import VFSReadResult

    step = WBSStep(
        step_number=1,
        target_role="core_dev",  # type: ignore[arg-type]
        action="edit_file",  # type: ignore[arg-type]
        target_file="main.py",
        description="Stub step.",
        status="pending",  # type: ignore[arg-type]
    )
    mission = MissionSpecification(
        outcome="o", scope=["main.py"], constraints=["c"],
        decisions=["d"], tasks=[step], checks=["k"],
    )
    state: Dict[str, Any] = {
        "task_id": "coder-skill-test",
        "mission_spec": mission,
        "current_step_id": 1,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }
    state.update(state_overrides)

    captured: Dict[str, Any] = {}

    async def _capture(*, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        captured["messages"] = messages
        return ""

    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content="def foo():\n    return 1\n"),
    ), patch(
        "tools.llm_gateway.LLMGateway.acomplete_with_thinking",
        new=AsyncMock(side_effect=_capture),
    ):
        from agents.coder import run_coder_node

        await run_coder_node(state)

    system_msgs = [m["content"] for m in captured.get("messages", []) if m.get("role") == "system"]
    return "\n".join(system_msgs)


async def test_coder_injects_active_skill_directive() -> None:
    skills = [{"id": "s1", "name": "TabsRule", "body": "Always indent with tabs."}]
    system_prompt = await _capture_coder_system_prompt({"active_skills": skills})
    assert "Always indent with tabs." in system_prompt
    assert "TabsRule" in system_prompt


async def test_coder_without_skills_has_no_skill_block() -> None:
    system_prompt = await _capture_coder_system_prompt({})
    assert 'kind="skill"' not in system_prompt
