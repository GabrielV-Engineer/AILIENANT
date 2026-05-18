"""Phase 4.5 — Chaos Crucible: end-to-end integration tests.

Spec source: Phase 4.5 Checkpoint Gate task brief.
Validates the convergence of Memory (Ph3), SQLite WAL (Ph2), LangGraph
Orchestration (Ph4.1-4.3), and Lifecycle Management (Ph4.4) under
chaotic conditions (network drops, double faults, phantom reconnects,
mid-flight crashes).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

pytestmark = pytest.mark.anyio


# =====================================================================
# A. VRAM & Memory Stress
# =====================================================================


async def test_kv_cache_release_on_mode_switch() -> None:
    """A1: SEQUENTIAL -> FULL_SWARM transition triggers KV cache release hook."""
    from brain import intent_router

    intent_router._last_dispatched_mode = None  # reset sentinel

    full_app = AsyncMock()
    full_app.ainvoke = AsyncMock(return_value={"messages": []})

    with patch(
        "core.lifecycle_manager.lifecycle_manager.release_vram_on_mode_switch",
        new_callable=AsyncMock,
    ) as release_mock, patch(
        "brain.fast_path.execute_sequential_bypass",
        new_callable=AsyncMock,
        return_value={"messages": []},
    ), patch("brain.swarms.build_full_swarm", return_value=full_app):
        await intent_router.process_user_intent(
            prompt="hi",
            workspace_root="/tmp",
            execution_mode="sequential",
        )
        await intent_router.process_user_intent(
            prompt="refactor everything",
            workspace_root="/tmp",
            execution_mode="full_swarm",
        )

    assert release_mock.await_count == 1, (
        "Mode-switch hook must fire exactly once on SEQUENTIAL->FULL_SWARM "
        f"transition; got {release_mock.await_count}"
    )


async def test_summarizer_protects_phase4_state() -> None:
    """A2 (per Phase 4.5 spec, corrected target): the message Summarizer
    (run_summarize_node, brain/summarizer.py) must compress oversized `messages`
    while leaving Phase 4 control channels (error_streak, active_role,
    circuit_breaker_tripped, cloud_surgeon_invocations) untouched.

    Original spec said 'Janitor (from Phase 3)' but core/janitor.py only purges
    LanceDB/MCTS rows and never touches graph state. The component that
    compresses `messages` over the 80% context threshold is the Summarizer.
    """
    from brain.state import LLMProfile
    from brain.summarizer import run_summarize_node

    big_chunk = "x" * 1000
    messages = [{"role": "assistant", "content": big_chunk} for _ in range(50)]

    phase4_fields = {
        "error_streak": 2,
        "active_role": "core_dev",
        "circuit_breaker_tripped": False,
        "cloud_surgeon_invocations": 0,
        "style_bypass_active": False,
    }

    state = {
        "messages": messages,
        "task_id": "chaos-a2",
        "active_llm_profile": LLMProfile(
            model_name="gpt-4",
            parameters_b=0.0,
            context_window=2048,
            quantization="fp16",
        ),
        **phase4_fields,
    }

    fake_response = type(
        "R",
        (),
        {
            "choices": [
                type(
                    "C",
                    (),
                    {"message": type("M", (), {"content": "compressed history"})()},
                )()
            ]
        },
    )()

    fake_decision = type(
        "D",
        (),
        {"cancelled": False, "effective_model": "small", "holds_lock": False},
    )()

    with patch(
        "brain.summarizer.LLMGateway.ainvoke",
        new_callable=AsyncMock,
        return_value=fake_response,
    ), patch(
        "brain.summarizer.ResourceBroker.acquire_or_resolve",
        new_callable=AsyncMock,
        return_value=fake_decision,
    ):
        delta = await run_summarize_node(state)

    # Compression fired: __replace__ sentinel present and shorter than input.
    assert "messages" in delta, "Summarizer should have compressed messages."
    assert isinstance(delta["messages"][0], dict) and delta["messages"][0].get(
        "__replace__"
    )
    assert len(delta["messages"]) < len(messages)

    # Phase 4 fields are NOT in the delta — Summarizer never writes them.
    for key in phase4_fields:
        assert key not in delta, f"Summarizer leaked Phase 4 field: {key}"


# =====================================================================
# B. Micro-Swarm Logic Traps
# =====================================================================


async def test_micro_swarm_double_fault_exhaustion() -> None:
    """B1: Coder + syntax both fail forever -> error_streak=3 -> CLOUD_SURGEON
    -> second fail -> CLOUD_SURGEON_EXHAUSTED flag -> graph exits to END.
    No infinite loop.
    """
    from brain.swarms import build_micro_swarm

    async def failing_coder(state):
        return {"code_under_validation": "this is not python {"}

    async def failing_syntax(state):
        return {"syntax_gate_status": "fail", "errors": ["intentional"]}

    async def passthrough_style(state):
        return {}

    with patch("agents.coder.run_coder_node", new=failing_coder), patch(
        "validators.gates.syntax_gate_node", new=failing_syntax
    ), patch("validators.gates.style_gate_node", new=passthrough_style):
        app = build_micro_swarm()
        result = await app.ainvoke(
            {
                "task_id": "chaos-b1",
                "user_input": "fix it",
                "messages": [],
                "execution_mode": "MICRO_SWARM",
                "error_streak": 0,
                "consecutive_style_failures": 0,
                "cloud_surgeon_invocations": 0,
                "circuit_breaker_tripped": False,
                "style_bypass_active": False,
                "syntax_gate_status": "pending",
                "style_gate_status": "pending",
            }
        )

    flags = result.get("security_flags", []) or []
    assert "CLOUD_SURGEON_EXHAUSTED" in flags, (
        f"Expected CLOUD_SURGEON_EXHAUSTED in security_flags; got {flags}"
    )
    assert result.get("cloud_surgeon_invocations", 0) == 1, (
        "Cloud Surgeon should fire exactly once (MAX_CLOUD_SURGEON=1)."
    )
    assert result.get("circuit_breaker_tripped") is True


async def test_micro_swarm_ugly_but_functional_bypass() -> None:
    """B2: Syntax passes, style fails twice -> style_bypass_active latches ->
    END. Cloud Surgeon is NOT invoked.
    """
    from brain.swarms import build_micro_swarm

    async def coder(state):
        return {"code_under_validation": "x = 1  # ugly"}

    async def syntax_pass(state):
        return {"syntax_gate_status": "pass"}

    async def style_fail_then_latch(state):
        new_count = int(state.get("consecutive_style_failures", 0)) + 1
        result = {"consecutive_style_failures": new_count, "errors": ["lint"]}
        if new_count >= 2:
            result["style_bypass_active"] = True
            result["security_flags"] = ["STYLE_BYPASS_ACTIVATED"]
        return result

    with patch("agents.coder.run_coder_node", new=coder), patch(
        "validators.gates.syntax_gate_node", new=syntax_pass
    ), patch("validators.gates.style_gate_node", new=style_fail_then_latch):
        app = build_micro_swarm()
        result = await app.ainvoke(
            {
                "task_id": "chaos-b2",
                "messages": [],
                "execution_mode": "MICRO_SWARM",
                "error_streak": 0,
                "consecutive_style_failures": 0,
                "cloud_surgeon_invocations": 0,
                "circuit_breaker_tripped": False,
                "style_bypass_active": False,
                "syntax_gate_status": "pending",
                "style_gate_status": "pending",
            }
        )

    assert result.get("style_bypass_active") is True
    assert result.get("cloud_surgeon_invocations", 0) == 0, (
        "Cloud Surgeon must NOT be invoked when only style failed."
    )
    assert "STYLE_BYPASS_ACTIVATED" in (result.get("security_flags") or [])


# =====================================================================
# C. Persistence Mid-Flight Crash
# =====================================================================


async def test_sqlite_wal_resumes_mid_swarm() -> None:
    """C1: Compile FULL_SWARM with MemorySaver + interrupt_before=['micro_swarm'].
    Run with thread_id=t. Resume with the same thread_id. Assert researcher_agent
    and planner_agent are NOT re-invoked on resume.
    """
    from brain.swarms import build_full_swarm

    researcher_calls = {"n": 0}
    planner_calls = {"n": 0}

    async def researcher(state):
        researcher_calls["n"] += 1
        return {"researcher_skeleton": "skeleton"}

    async def planner(state):
        planner_calls["n"] += 1
        return {"mission_spec": None}

    async def orchestrator(state):
        return {}

    async def env(state):
        return {}

    async def analyst(state):
        return {"messages": [{"role": "assistant", "content": "done"}]}

    with patch("agents.researcher.run_researcher_node", new=researcher), patch(
        "agents.planner.run_planner_node", new=planner
    ), patch("agents.orchestrator.run_orchestrator_node", new=orchestrator), patch(
        "validators.environment.verify_environment_node", new=env
    ), patch(
        "agents.analyst.run_analyst_node", new=analyst
    ):
        saver = MemorySaver()
        app = build_full_swarm(saver, interrupt_before=["micro_swarm"])
        config = {"configurable": {"thread_id": "chaos-c1"}}
        await app.ainvoke(
            {
                "task_id": "chaos-c1",
                "messages": [],
                "execution_mode": "FULL_SWARM",
            },
            config=config,
        )

        assert researcher_calls["n"] == 1
        assert planner_calls["n"] == 1

        # Resume with same thread_id and same compiled app — should pick up
        # at micro_swarm, NOT re-run researcher or planner.
        await app.ainvoke(None, config=config)

    assert researcher_calls["n"] == 1, (
        f"Researcher must not re-run on resume; got {researcher_calls['n']} calls"
    )
    assert planner_calls["n"] == 1, (
        f"Planner must not re-run on resume; got {planner_calls['n']} calls"
    )


# =====================================================================
# D. Lifecycle Phantom Reconnects
# =====================================================================


async def test_lifecycle_debounce_prevents_vram_purge() -> None:
    """D1: shutdown_workspace schedules VRAM purge after debounce_sec.
    If register_task is called for the same PID within the window, the timer
    is cancelled and _release_vram is NEVER called.
    """
    from core.lifecycle_manager import WorkspaceLifecycleManager

    mgr = WorkspaceLifecycleManager(debounce_sec=0.05)

    with patch.object(mgr, "_release_vram", new_callable=AsyncMock) as release_mock:
        await mgr.shutdown_workspace(4242)

        # Phantom reconnect well within the debounce window.
        await asyncio.sleep(0.01)
        loop_task = asyncio.create_task(asyncio.sleep(0))
        mgr.register_task(4242, loop_task)

        # Wait past the original window. The TimerHandle should be cancelled.
        await asyncio.sleep(0.10)
        await loop_task

    assert release_mock.await_count == 0, (
        f"Debounce failed: _release_vram called {release_mock.await_count} times "
        "despite reconnect within window."
    )
