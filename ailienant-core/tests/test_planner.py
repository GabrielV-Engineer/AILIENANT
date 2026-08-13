# tests/test_planner.py
"""Phase 4.1.2 DoD — PlannerAgent gap closure tests.

Covers the three new behaviours:
  1. Bounded ValidationError retry (1 retry → success).
  2. Retry exhaustion → clean error return (no fatal crash).
  3. researcher_skeleton consumption (Phase 4.1.1 channel wired into the prompt).

All tests use the established anyio + AsyncMock + patch pattern from
tests/test_fast_boot.py:191-262.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.runnables import RunnableConfig

from agents.recency import session_heatmap
from brain.state import MissionSpecification, WBSStep
from core.response_cache import response_cache


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_heatmap() -> Any:
    """Keep the process-singleton recency heatmap isolated between tests."""
    session_heatmap.reset()
    yield
    session_heatmap.reset()


@pytest.fixture(autouse=True)
def _reset_response_cache() -> Any:
    """Isolate the process-singleton semantic response cache between tests.

    A planner turn stores its validated plan keyed by the stable inputs; without
    this reset an identical-key turn in a sibling test would be served from cache
    and skip the gateway, masking retry/skeleton behaviour."""
    response_cache.clear()
    yield
    response_cache.clear()


def _valid_mission_json() -> str:
    """Minimal MissionSpecification that satisfies the strict Pydantic schema."""
    return MissionSpecification(
        outcome="Test outcome.",
        scope=["test/scope.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="architect_refactor",
                action="read_file",
                target_file="test/scope.py",
                description="Stub task.",
            )
        ],
        checks=["Pytest exits 0."],
    ).model_dump_json()


def _make_response(content: str) -> MagicMock:
    """Shape an LLMGateway.ainvoke response mock around a string body."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


def _broker_decision() -> MagicMock:
    """Stub a ResourceBroker decision: lock not held, not cancelled, BIG tier."""
    decision = MagicMock()
    decision.cancelled = False
    decision.effective_model = "ailienant/big"
    decision.holds_lock = False
    return decision


def _base_state(**overrides: Any) -> Dict[str, Any]:
    """Minimal AIlienantGraphState slice that satisfies run_planner_node."""
    state: Dict[str, Any] = {
        "task_id": "planner-test",
        "user_input": "Add a feature.",
        "workspace_root": "/ws",
        "project_id": "abc123",
        "context_metrics": None,
        "mission_spec": None,
        "immutable_wbs": None,
        "errors": [],
        "retry_count": 0,
        "current_cost_usd": 0.0,
        "max_budget_usd": 10.0,
        "vfs_buffer": {},
        "terminal_output": "",
        "parallel_tasks": [],
        "tci": 45.0,
        "css": 78.5,
        "provider": "LOCAL",
        "current_step_id": None,
        "dirty_buffers": [],
        "ide_context": "",
        "researcher_skeleton": None,
    }
    state.update(overrides)
    return state


# ── Test 1: retry-then-succeed ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_retries_on_malformed_json_then_succeeds() -> None:
    """First LLM call returns garbage; second returns valid JSON.
    Planner must consume exactly 1 retry and succeed."""

    bad_response = _make_response("{ this is not valid json ")
    good_response = _make_response(_valid_mission_json())

    mock_ainvoke = AsyncMock(side_effect=[bad_response, good_response])
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    assert result.get("planner_retry_count") == 1
    assert mock_ainvoke.call_count == 2

    # The second call's user message must contain the corrective banner.
    # Phase 7.10.4 (ADR-704) — corrective now names the envelope failure mode + feeds errors.
    second_call_messages: List[Dict[str, str]] = mock_ainvoke.call_args_list[1].kwargs[
        "messages"
    ]
    corrective = second_call_messages[-1]["content"]
    assert "failed schema validation with these errors" in corrective
    assert "DO NOT wrap it in any top-level key" in corrective


# ── Test 1b: Actor-Critic narration surfaces on the ideation→planner handoff ───


@pytest.mark.anyio
async def test_planner_narrates_critic_cycle_on_handoff_brief() -> None:
    """A distilled brief (the ideation handoff) drives the planner's reflection loop;
    the critic cycle must surface in the action log: review → rejected → validated."""
    captured: List[str] = []

    async def _narrate(node_name: str, step_id: Any = None) -> None:
        captured.append(node_name)

    cfg: RunnableConfig = {"configurable": {"narrate": _narrate}}

    bad_response = _make_response("{ not json ")
    good_response = _make_response(_valid_mission_json())
    mock_ainvoke = AsyncMock(side_effect=[bad_response, good_response])
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    # The brief the ideation handoff folds into user_input, plus the glossary it carries.
    state = _base_state(
        user_input="Build a JWT auth service.\n\nConstraints:\n- No new deps.",
        ideation_glossary={"token": "a signed JWT"},
    )

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state, cfg)

    mission = result.get("mission_spec")
    assert mission is not None
    # The critic cycle is legible in the action log.
    assert "critic_review" in captured
    assert any(c.startswith("critic_rejected → replanning") for c in captured)
    assert "plan_validated" in captured
    # The Socratic glossary survived the handoff into the final plan.
    assert mission.ubiquitous_language.get("token") == "a signed JWT"


# ── Test 2: retries exhausted → clean error return ────────────────────────────


@pytest.mark.anyio
async def test_planner_returns_errors_when_retries_exhausted() -> None:
    """All three attempts return garbage. Planner must surface a clean error,
    never raise, and never produce a mission_spec."""

    garbage = _make_response("definitely not json")
    mock_ainvoke = AsyncMock(side_effect=[garbage, garbage, garbage])
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is None
    assert "errors" in result and result["errors"]
    assert "schema validation exhausted" in result["errors"][0]
    assert mock_ainvoke.call_count == 3
    assert result.get("planner_retry_count") == 3


# ── Test 3: researcher_skeleton consumption ───────────────────────────────────


@pytest.mark.anyio
async def test_planner_consumes_researcher_skeleton() -> None:
    """When researcher_skeleton is present in state, its content must appear
    inside the prompt sent to LLMGateway.ainvoke."""

    skeleton = "## Skeleton\n- core/auth.py: handles JWT validation"
    good_response = _make_response(_valid_mission_json())

    mock_ainvoke = AsyncMock(return_value=good_response)
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state(researcher_skeleton=skeleton)

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    mock_ainvoke.assert_called_once()

    # The skeleton text must surface in either the system prompt or user message.
    sent_messages: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    joined = "\n".join(m["content"] for m in sent_messages)
    assert "core/auth.py: handles JWT validation" in joined


# ── Test 4: active_skills injection ───────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_injects_active_skills() -> None:
    """A resolved skill on state['active_skills'] must surface (sandboxed) in the
    prompt sent to the gateway; an empty list injects nothing."""

    skill_body = "Always check for SQL injection in query builders."
    good_response = _make_response(_valid_mission_json())

    mock_ainvoke = AsyncMock(return_value=good_response)
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state(
        active_skills=[{"id": "s1", "name": "SecAudit", "body": skill_body}]
    )

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state)

    assert result.get("mission_spec") is not None
    sent_messages: List[Dict[str, str]] = mock_ainvoke.call_args.kwargs["messages"]
    joined = "\n".join(m["content"] for m in sent_messages)
    assert skill_body in joined
    assert 'kind="skill"' in joined  # rendered inside the sandboxed directive block


# ── Test 5: live plan-of-attack reasoning pass (non-native models) ─────────────


def _fake_astream_reasoning_factory(reasoning_chunks: List[str], draft_json: str):
    """Build a fake ``astream_reasoning`` async generator.

    Branch on the call shape so a single fake serves BOTH callers in a thinking-on
    turn: the free-form reasoning pass (``free_form_answer=True``, no
    ``response_format``) yields incremental ``thinking`` deltas; the strict WBS
    draft (``response_format`` set, routed through here by ``acomplete_with_thinking``)
    yields the plan JSON as one ``text`` delta.
    """
    from tools.stream_delta import StreamDelta

    def _fake(messages, tier="medium", *, temperature=0.0, max_tokens=4096,
              timeout=60.0, session_id=None, thinking_budget_tokens=4096,
              response_format=None, free_form_answer=False):
        async def _gen():
            if response_format is None and free_form_answer:
                for chunk in reasoning_chunks:
                    yield StreamDelta("thinking", chunk, "simulated")
            else:
                yield StreamDelta("text", draft_json, "simulated")
        return _gen()

    return _fake


@pytest.mark.anyio
async def test_planner_streams_reasoning_before_draft_nonnative() -> None:
    """On a non-native model with a wired sink, the planner streams multiple
    incremental reasoning deltas to the Thought Box BEFORE the WBS is drafted,
    and the resulting MissionSpecification is intact."""
    collected: List[tuple[str, str]] = []

    async def _sink(text: str, source: str) -> None:
        collected.append((text, source))

    cfg: RunnableConfig = {
        "configurable": {
            "stream_thinking": _sink,
            "enable_native_thinking": True,
            "thinking_budget_tokens": 4096,
        }
    }

    fake = _fake_astream_reasoning_factory(
        ["Let me think about ", "the approach ", "and the files."],
        _valid_mission_json(),
    )
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.astream_reasoning", fake
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ), patch(
        "core.config.model_resolver.get_chat_target",
        return_value=MagicMock(model="ollama/llama3"),
    ), patch(
        "tools.llm_gateway._supports_native_thinking", return_value=False
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state, cfg)

    # Reasoning streamed incrementally (multiple deltas, not one final flush).
    reasoning_deltas = [t for t, _ in collected]
    assert len(reasoning_deltas) >= 2
    assert "".join(reasoning_deltas).startswith("Let me think about")
    # The strict draft still produced a valid, intact plan.
    assert result.get("mission_spec") is not None


@pytest.mark.anyio
async def test_planner_skips_reasoning_pass_on_native_model() -> None:
    """On a native model the extra reasoning pass must NOT fire (native models
    already stream reasoning during their own draft — no double pass)."""
    collected: List[tuple[str, str]] = []

    async def _sink(text: str, source: str) -> None:
        collected.append((text, source))

    cfg: RunnableConfig = {
        "configurable": {
            "stream_thinking": _sink,
            "enable_native_thinking": True,
            "thinking_budget_tokens": 4096,
        }
    }

    fake = _fake_astream_reasoning_factory(
        ["should NOT be emitted"],
        _valid_mission_json(),
    )
    mock_acquire = AsyncMock(return_value=_broker_decision())
    mock_release = AsyncMock(return_value=None)

    state = _base_state()

    with patch("agents.planner.DEBUG_MODE", False), patch(
        "core.memory.trajectory_memory.TrajectoryMemoryManager"
    ) as mock_traj_cls, patch(
        "tools.llm_gateway.LLMGateway.astream_reasoning", fake
    ), patch(
        "agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire
    ), patch(
        "agents.planner.ResourceBroker.release", mock_release
    ), patch(
        "core.config.model_resolver.get_chat_target",
        return_value=MagicMock(model="claude-sonnet-5"),
    ), patch(
        "tools.llm_gateway._supports_native_thinking", return_value=True
    ):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])

        from agents.planner import run_planner_node

        result = await run_planner_node(state, cfg)

    # The reasoning pass was gated out — the draft's own stream yields no 'thinking'.
    assert collected == []
    assert result.get("mission_spec") is not None
