"""Resilience & Observability — checkpoint gate.

Sibling-gate convention (test-only). Asserts the division's contract:
  A. Fast Track — a trivial query is detected lexically (pre-RAG).
  B. derive_routing_decision — fast_track short-circuits to LOCAL_SMALL ahead of
     the CSS floor; the existing CSS/TCI matrix is otherwise preserved.
  C. hardware_reroute — LOCAL_* degrades on a VRAM floor / predicted overflow,
     to cloud when reachable, else LOCAL_SMALL + warning (never blocks).
  D. estimate_graph_weight — overflow is judged against the candidate model's
     real window, not a cloud default.
  E. configure_langsmith — off by default; no new sink.
  F. effective_vram_gb — discrete VRAM vs Apple-Silicon unified memory.
  G. Planner integration — a trivial query skips GraphRAG and routes LOCAL_SMALL
     with is_red_alert False; a VRAM-starved host reroutes a LOCAL decision.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import ContextMeter, LLMProfile, MissionSpecification, WBSStep
from core.graph_weight import estimate_graph_weight
from core.memory.context_auditor import (
    compute_task_complexity_index,
    derive_routing_decision,
    hardware_reroute,
    RiskLevel,
)
from core.observability import configure_langsmith
from shared.hardware import HardwareProfile, effective_vram_gb

pytestmark = pytest.mark.anyio


def _profile(vram_gb: float = 24.0, *, used: float = 0.0, apple: bool = False,
             ram_avail: float = 0.0) -> HardwareProfile:
    return HardwareProfile(
        os_type="macos" if apple else "windows",
        is_apple_silicon=apple,
        vram_gb=vram_gb,
        vram_used_gb=used,
        ram_available_gb=ram_avail,
    )


# ── A. Structural task-complexity score ──────────────────────────────────────
# Replaces the Fast Track lexical probe, which was deleted: its only caller was
# the researcher node, which runs exclusively on coding turns, so the "is this a
# trivial self-contained question?" test it performed was structurally never true
# where it was asked. TCI is what actually differentiates a turn now.


def test_tci_scores_a_broad_greenfield_request_above_the_small_band() -> None:
    """The reported incident: a whole-stack build on an empty workspace was
    scored 0.0 and routed to the smallest model."""
    tci = compute_task_complexity_index(
        user_input=(
            "prepare our ecosystem first, our stack, for a landing page. i want to "
            "use javascript, next.js and react, also create the instructions file "
            "with a list of tasks and restrictions, then plan the full wbs taking "
            "in account frontend, backend, SEO and security"
        ),
        corpus_empty=True,
    )
    assert tci >= 30.0
    assert derive_routing_decision(tci, 100.0, corpus_empty=True) != "LOCAL_SMALL"


def test_tci_keeps_a_trivial_edit_on_the_cheap_path() -> None:
    tci = compute_task_complexity_index(user_input="fix the typo", retrieved_files=1)
    assert tci < 30.0
    assert derive_routing_decision(tci, 100.0) == "LOCAL_SMALL"


def test_tci_is_bounded_and_never_raises_on_hostile_input() -> None:
    """user_input is untrusted (§6.2) and this runs on every turn, so a
    pasted megabyte or a pathological punctuation run must stay bounded."""
    hostile = [
        "",
        "x" * 200_000,
        chr(0) + chr(0xFEFF) + "  ",
        "(" * 50_000,
        "and " * 50_000,
    ]
    for text in hostile:
        score = compute_task_complexity_index(user_input=text)
        assert 0.0 <= score <= 100.0


# ── B. derive_routing_decision ───────────────────────────────────────────────

@pytest.mark.parametrize("tci,css,expected", [
    (10.0, 80.0, "LOCAL_SMALL"),
    (50.0, 80.0, "LOCAL_BIG"),
    (90.0, 80.0, "CLOUD"),
    (10.0, 30.0, "CLOUD"),   # css floor
])
def test_derive_matrix_preserved(tci: float, css: float, expected: str) -> None:
    assert derive_routing_decision(tci, css) == expected


@pytest.mark.parametrize("tci,css,expected", [
    (29.999, 80.0, "LOCAL_SMALL"),   # just below the SMALL/MEDIUM boundary
    (30.0, 80.0, "LOCAL_MEDIUM"),    # boundary itself is MEDIUM's floor
    (40.0, 80.0, "LOCAL_MEDIUM"),    # squarely inside the new band
    (49.999, 80.0, "LOCAL_MEDIUM"),  # just below the MEDIUM/BIG boundary
    (50.0, 80.0, "LOCAL_BIG"),       # boundary itself is BIG's floor — unchanged
    (74.999, 80.0, "LOCAL_BIG"),     # just below the BIG/CLOUD boundary
])
def test_derive_local_medium_band(tci: float, css: float, expected: str) -> None:
    """LOCAL_MEDIUM was carved out of the interior of the old single 30-75
    LOCAL_BIG band; every pre-existing boundary (30, 75) and the historically
    pinned tci=50→LOCAL_BIG verdict above must survive unchanged — this is
    what makes a genuinely useless routing tier (nothing ever selected it)
    into a real destination for moderate-complexity turns."""
    assert derive_routing_decision(tci, css) == expected


# ── C. hardware_reroute ──────────────────────────────────────────────────────

def test_reroute_vram_floor_to_cloud() -> None:
    routing, provider, warning = hardware_reroute(
        "LOCAL_SMALL", "LOCAL", _profile(vram_gb=0.5), cloud_available=True,
    )
    assert routing == "CLOUD" and provider == "CLOUD"
    assert warning is not None and "VRAM" in warning


def test_reroute_vram_floor_no_cloud_degrades_with_warning() -> None:
    routing, provider, warning = hardware_reroute(
        "LOCAL_BIG", "LOCAL", _profile(vram_gb=0.5), cloud_available=False,
    )
    assert routing == "LOCAL_SMALL" and provider == "LOCAL"
    assert warning is not None and "no cloud" in warning.lower()


def test_reroute_overflow_risk_to_cloud_even_with_vram() -> None:
    routing, _provider, warning = hardware_reroute(
        "LOCAL_BIG", "LOCAL", _profile(vram_gb=24.0),
        cloud_available=True, overflow_risk=True,
    )
    assert routing == "CLOUD"
    assert warning is not None and "overflow" in warning.lower()


def test_reroute_healthy_local_is_passthrough() -> None:
    out = hardware_reroute("LOCAL_SMALL", "LOCAL", _profile(vram_gb=24.0),
                           cloud_available=True)
    assert out == ("LOCAL_SMALL", "LOCAL", None)


def test_reroute_cloud_decision_is_passthrough() -> None:
    out = hardware_reroute("CLOUD", "CLOUD", _profile(vram_gb=0.5),
                           cloud_available=True)
    assert out == ("CLOUD", "CLOUD", None)


def test_reroute_missing_profile_is_passthrough() -> None:
    out = hardware_reroute("LOCAL_SMALL", "LOCAL", None, cloud_available=True)
    assert out == ("LOCAL_SMALL", "LOCAL", None)


# ── D. estimate_graph_weight (candidate-window correctness) ──────────────────

def test_weight_overflow_judged_against_local_window_not_cloud() -> None:
    big_text = "word " * 10000  # ~10k tokens → over an 8k local budget, under 128k
    state = {"messages": [{"role": "user", "content": big_text}]}

    local = estimate_graph_weight(state, model_context_window=8192)
    cloud = estimate_graph_weight(state, model_context_window=128_000)

    assert local.overflow_risk is True, "must flag overflow against the local window"
    assert cloud.overflow_risk is False, "the same state is safe in a 128k window"


def test_weight_empty_state_is_zero() -> None:
    est = estimate_graph_weight({}, model_context_window=8192)
    assert est.estimated_tokens == 0 and est.overflow_risk is False


# ── E. configure_langsmith (off by default, no sink) ─────────────────────────

def test_langsmith_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert configure_langsmith() is False


def test_langsmith_on_when_env_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test-key")
    assert configure_langsmith() is True


def test_langsmith_off_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    assert configure_langsmith() is False


# ── F. effective_vram_gb ─────────────────────────────────────────────────────

def test_effective_vram_discrete_is_free_vram() -> None:
    assert effective_vram_gb(_profile(vram_gb=24.0, used=8.0)) == pytest.approx(16.0)


def test_effective_vram_apple_silicon_uses_ram() -> None:
    p = _profile(vram_gb=0.0, apple=True, ram_avail=18.0)
    assert effective_vram_gb(p) == pytest.approx(18.0)


# ── G. Planner integration ───────────────────────────────────────────────────

def _make_mission(outcome: str) -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome, scope=["a.py"], constraints=[], decisions=[],
        tasks=[WBSStep(step_number=1, target_role="architect_refactor",
                       action="read_file", target_file="a.py",
                       description="d", status="pending")],
        checks=["c"],
    )


def _ctx() -> ContextMeter:
    return ContextMeter(
        semantic_similarity=0.0, graph_coverage=0.0, recency_score=0.4,
        css_total=100.0, task_complexity_index=10.0,
        routing_decision="LOCAL_SMALL", is_red_alert=False,
    )


def _state(user_input: str, **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "task_id": "p82-gate",
        "user_input": user_input,
        "workspace_root": "/tmp/ws_p82",
        "project_id": "proj82",
        "context_metrics": _ctx(),
        "mission_spec": None,
        "errors": [],
        "retry_count": 0,
        "current_cost_usd": 0.0,
        "max_budget_usd": 10.0,
        "vfs_buffer": {},
        "terminal_output": "",
        "parallel_tasks": [],
        "tci": 10.0,
        "css": 100.0,
        "provider": "LOCAL",
        "current_step_id": None,
        "dirty_buffers": [],
        "ide_context": "",
    }
    base.update(extra)
    return base


def _planner_mocks(sem: float = 0.2, coverage: float = 0.1) -> Tuple[AsyncMock, AsyncMock, AsyncMock, Any]:
    search = AsyncMock(return_value=(sem, ["a.py"], [""]))
    deep = AsyncMock(return_value=MagicMock(
        coverage_ratio=coverage, context_block="", parsed_files=["a.py"], target_files=["a.py"]))
    audit = AsyncMock(return_value=RiskLevel.NONE)
    llm = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=_make_mission("done").model_dump_json()))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)))
    return search, deep, audit, llm


async def _noop_reasoner(_m: Any) -> str:
    """Grounding-loop reasoner that runs no tools (cascade tests stay focused)."""
    return "{}"


# Routing/retrieval now live in the Researcher node; these cascade vectors drive
# run_researcher_node with the grounding loop neutralized.
from langchain_core.runnables import RunnableConfig  # noqa: E402

_RCFG: RunnableConfig = {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}


async def test_researcher_runs_retrieval_and_the_judge_on_every_coding_turn() -> None:
    """Replaces the Fast Track row. The researcher node only ever runs on coding
    turns, so the "trivial self-contained question" short-circuit could never be
    correct there — and when it did fire (on a plan-acceptance phrase) it skipped
    GraphRAG, bypassed the Mini-Judge and pinned the cheapest tier."""
    search, deep, audit, llm = _planner_mocks()
    state = _state("hello, what is recursion?")

    with patch("agents.researcher.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=None), \
         patch("core.state_manager.dump_state_to_markdown", return_value=True), \
         patch("agents.researcher.audit_task_complexity", new=audit), \
         patch("core.memory.semantic_memory.SemanticMemoryManager") as sem_cls, \
         patch("core.memory.graphrag_extractor.GraphRAGDynamicExtractor") as extr_cls, \
         patch("tools.llm_gateway.LLMGateway.ainvoke", new=llm):
        extr_cls.return_value.deep_parse = deep
        sem_cls.return_value.search_with_paths = search

        from agents.researcher import run_researcher_node
        result = await run_researcher_node(state)

    search.assert_called()
    deep.assert_called()
    audit.assert_called()  # the Mini-Judge is no longer bypassable
    ctx: ContextMeter = result["context_metrics"]
    assert ctx.routing_decision in {"LOCAL_SMALL", "LOCAL_MEDIUM", "LOCAL_BIG", "CLOUD"}


async def test_planner_low_vram_reroutes_local_to_cloud() -> None:
    # High sem/coverage keeps CSS well above the red-alert floor so the cascade
    # resolves LOCAL_SMALL (tci=10) — the hardware reroute is then the sole cause
    # of the CLOUD decision, not the CSS gate.
    search, deep, audit, llm = _planner_mocks(sem=0.9, coverage=0.9)
    state = _state(
        "refactor the authentication module",
        hardware_profile=_profile(vram_gb=0.5),
        active_llm_profile=LLMProfile(
            model_name="ailienant/small", parameters_b=1.5,
            context_window=8192, quantization="q4_0"),
    )

    with patch("agents.researcher.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=None), \
         patch("core.state_manager.dump_state_to_markdown", return_value=True), \
         patch("agents.researcher.audit_task_complexity", new=audit), \
         patch("agents.researcher.check_cloud_availability", return_value=True), \
         patch("tools.researcher_tools.build_researcher_tools", return_value={}), \
         patch("core.memory.semantic_memory.SemanticMemoryManager") as sem_cls, \
         patch("core.memory.graphrag_extractor.GraphRAGDynamicExtractor") as extr_cls, \
         patch("tools.llm_gateway.LLMGateway.ainvoke", new=llm):
        extr_cls.return_value.deep_parse = deep
        sem_cls.return_value.search_with_paths = search

        from agents.researcher import run_researcher_node
        result = await run_researcher_node(state, _RCFG)

    assert result["context_metrics"].routing_decision == "CLOUD"
    assert result["routing_warning"] is not None
