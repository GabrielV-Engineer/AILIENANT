"""Checkpoint gate for Researcher node promotion + retrieval/routing consolidation.

Group 1 (DEBT-069): the Researcher drives a bounded READ_ONLY dispatch loop.
Group 2: the Researcher emits the routing signal (context_metrics / css / provider /
  routing_decision); fast_track pins css=100 and skips the loop.
Group 3: the compiled graph reaches researcher_agent before planner_agent, and the
  router verdicts are unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Dict, Iterator, List, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.tools import BaseTool

from brain.state import ContextMeter
from core.memory.context_auditor import RiskLevel
from core.permissions import ToolPrivilegeTier


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


class _FakeGlob(BaseTool):
    name: str = "glob"
    description: str = "List files."

    def _run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def _arun(self, pattern: str = "", **_: Any) -> str:
        return "src/a.py\nsrc/b.py"


def _fake_tool_map() -> Dict[str, Any]:
    from core.tool_dispatch import RegisteredTool

    return {
        "glob": RegisteredTool(
            _FakeGlob(), ToolPrivilegeTier.READ_ONLY, frozenset({"researcher"})
        )
    }


def _glob_then_stop() -> Any:
    """A dispatch reasoner: emit one glob call, then an empty envelope (stop)."""
    calls = {"n": 0}

    async def _reason(_messages: Sequence[Dict[str, Any]]) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool_calls":[{"name":"glob","args":{"pattern":"**/*.py"}}]}'
        return "{}"

    return _reason


def _llm_skeleton(text: str = "## Skeleton\n- src/a.py: foo() -> int") -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "rsrch-gate",
        "user_input": "Refactor the auth module for clarity.",
        "workspace_root": "/ws",
        "project_id": "proj-1",
        "explicit_mentions": [],
        "session_permission_mode": "AUTO",
        "errors": [],
    }
    state.update(overrides)
    return state


@contextlib.contextmanager
def _researcher_env(
    *, fast_track: bool, risk: RiskLevel = RiskLevel.NONE
) -> Iterator[Dict[str, Any]]:
    """Seal the researcher's heavy boundaries: DEBUG off, tools faked, LLM + mini-judge mocked."""
    mocks: Dict[str, Any] = {}
    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=fast_track
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value=_fake_tool_map()
    ), patch(
        "agents.researcher.audit_task_complexity",
        new=AsyncMock(return_value=risk),
    ), patch(
        # Seal the fast-boot snapshot: force the live retrieval path and never write /ws.
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "agents.researcher.LLMGateway.ainvoke", return_value=_llm_skeleton()
    ) as m_llm:
        mocks["llm"] = m_llm
        yield mocks


def _run(state: Dict[str, Any], configurable: Dict[str, Any]) -> Dict[str, Any]:
    from agents.researcher import run_researcher_node

    return asyncio.run(run_researcher_node(state, {"configurable": configurable}))


# ──────────────────────────────────────────────────────────────────────────────
# Group 1 — bounded READ_ONLY dispatch loop (DEBT-069)
# ──────────────────────────────────────────────────────────────────────────────


def test_researcher_drives_dispatch_loop() -> None:
    with _researcher_env(fast_track=False):
        result = _run(
            _base_state(),
            {"researcher_tool_reasoner": _glob_then_stop()},
        )
    trace = result.get("tool_dispatch_trace") or []
    assert [c["name"] for c in trace] == ["glob"]
    assert result["researcher_skeleton"].startswith("## Skeleton")


def test_researcher_loop_skipped_on_fast_track() -> None:
    with _researcher_env(fast_track=True):
        result = _run(
            _base_state(),
            {"researcher_tool_reasoner": _glob_then_stop()},
        )
    # fast_track short-circuits the grounding loop entirely.
    assert "tool_dispatch_trace" not in result


# ──────────────────────────────────────────────────────────────────────────────
# Group 2 — routing signal emission
# ──────────────────────────────────────────────────────────────────────────────


def test_researcher_emits_routing_signal() -> None:
    fake_search = AsyncMock(return_value=(0.8, ["src/a.py"], [""]))
    fake_deep = AsyncMock(
        return_value=MagicMock(
            context_block="STUB_BLOCK",
            coverage_ratio=1.0,
            parsed_files=["src/a.py"],
            target_files=["src/a.py"],
        )
    )
    with _researcher_env(fast_track=False):
        result = _run(
            # Seed a non-None context_metrics so the retrieval block runs.
            _base_state(
                context_metrics=ContextMeter(
                    semantic_similarity=0.0,
                    graph_coverage=0.0,
                    recency_score=0.0,
                    css_total=50.0,
                    task_complexity_index=0.0,
                    routing_decision="LOCAL_SMALL",
                    is_red_alert=False,
                )
            ),
            {
                "researcher_tool_reasoner": _glob_then_stop(),
                "planner_retrieval_fn": fake_search,
                "graph_fn": fake_deep,
            },
        )
    cm = result["context_metrics"]
    assert isinstance(cm, ContextMeter)
    assert cm.routing_decision in {"LOCAL_SMALL", "LOCAL_BIG", "CLOUD"}
    assert result["provider"] in {"LOCAL", "CLOUD"}
    assert "css" in result and "tci" in result
    fake_search.assert_awaited_once()
    fake_deep.assert_awaited_once()


def test_researcher_fast_track_pins_css() -> None:
    with _researcher_env(fast_track=True):
        result = _run(_base_state(), {})
    cm = result["context_metrics"]
    assert isinstance(cm, ContextMeter)
    assert result["css"] == 100.0
    assert cm.css_total == 100.0
    assert cm.is_red_alert is False
    assert result["provider"] == "LOCAL"


def test_researcher_always_emits_context_metrics_on_llm_failure() -> None:
    # Even when the skeleton LLM call fails, a non-None routing signal must surface.
    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=True
    ), patch(
        "agents.researcher.LLMGateway.ainvoke", side_effect=RuntimeError("byom down")
    ):
        result = _run(_base_state(), {})
    assert isinstance(result["context_metrics"], ContextMeter)
    assert result.get("errors")


# ──────────────────────────────────────────────────────────────────────────────
# Group 3 — graph wiring
# ──────────────────────────────────────────────────────────────────────────────


def test_engine_routes_through_researcher_before_planner() -> None:
    from brain.engine import alienant_app, route_after_summarize, route_after_ideation

    graph = alienant_app.get_graph()
    node_ids = set(graph.nodes.keys())
    assert "researcher_agent" in node_ids

    edges = {(e.source, e.target) for e in graph.edges}
    assert ("researcher_agent", "planner_agent") in edges

    # Router verdicts are unchanged — the path-map remap reroutes them, the routers
    # themselves still name planner_agent.
    assert route_after_summarize({"planner_mode_active": False}) == "planner_agent"
    assert route_after_ideation({"ideation_synthesized": True}) == "planner_agent"
