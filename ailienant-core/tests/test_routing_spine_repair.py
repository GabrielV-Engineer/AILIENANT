"""Regression coverage for the routing-spine repair.

Every case here corresponds to a defect found while root-causing a live session
that ended with "Drafted a plan but produced no concrete edits" — a schema-valid
MissionSpecification carrying zero WBS steps, drafted by a 3B model.

The through-line: several signals were declared, persisted and rendered but never
actually computed or consumed, so the failure was invisible at every layer it
passed through.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from core.memory.context_auditor import (
    RiskLevel,
    audit_task_complexity,
    compute_task_complexity_index,
    derive_routing_decision,
    resolve_model_alias_for_routing,
    tci_floor_for_tier,
)
from core.task_service import TaskPayload, TaskService
from shared.config import MODEL_BIG, MODEL_CLOUD, MODEL_MEDIUM, MODEL_SMALL

pytestmark = pytest.mark.anyio


# ── The incident prompt, verbatim in shape ───────────────────────────────────
_GREENFIELD = (
    "in this task i want to prepare our ecosystem first, our stack. for a vanilla "
    "landing page for my dev portfolio. i want to use javascript, next.js and react, "
    "also create the instructions file with a list of tasks and restrictions, then i "
    "want to plan the full wbs taking in account frontend, backend, SEO and security"
)


# ══════════════════════════════════════════════════════════════════════════════
# D2 — task_complexity_index was never computed
# ══════════════════════════════════════════════════════════════════════════════


def test_greenfield_build_no_longer_scores_as_trivial() -> None:
    """The whole defect in one assertion: this request scored 0.0, which routed a
    multi-domain project build to the smallest local model."""
    tci = compute_task_complexity_index(user_input=_GREENFIELD, corpus_empty=True)
    assert tci >= 30.0
    assert derive_routing_decision(tci, 100.0, corpus_empty=True) != "LOCAL_SMALL"


def test_empty_corpus_raises_complexity_rather_than_lowering_it() -> None:
    """Building from nothing is maximal work. Treating "nothing to retrieve" as
    "nothing to do" is what sent a from-scratch project to the cheapest tier."""
    green = compute_task_complexity_index(user_input="create the whole app", corpus_empty=True)
    brown = compute_task_complexity_index(user_input="create the whole app", corpus_empty=False)
    assert green > brown


def test_trivial_edits_still_take_the_cheap_path() -> None:
    """The fix must not simply escalate everything — that would just move the cost."""
    tci = compute_task_complexity_index(user_input="fix the typo", retrieved_files=1)
    assert derive_routing_decision(tci, 100.0) == "LOCAL_SMALL"


def test_breadth_raises_complexity_monotonically() -> None:
    scores = [
        compute_task_complexity_index(user_input="update the module", retrieved_files=n)
        for n in (0, 2, 6, 12)
    ]
    assert scores == sorted(scores)


def test_index_is_bounded_and_linear_on_hostile_input() -> None:
    """user_input is untrusted (§6.2) and this runs on every turn: a pasted
    megabyte must cost a bounded slice, never a proportional stall."""
    for text in ("", "x" * 500_000, chr(0) + chr(0xFEFF), "(" * 100_000, "and " * 100_000):
        assert 0.0 <= compute_task_complexity_index(user_input=text) <= 100.0


def test_band_boundaries_are_untouched() -> None:
    """TCI places a turn WITHIN the committed bands; it must never redefine them."""
    assert derive_routing_decision(29.99, 80.0) == "LOCAL_SMALL"
    assert derive_routing_decision(30.0, 80.0) == "LOCAL_MEDIUM"
    assert derive_routing_decision(50.0, 80.0) == "LOCAL_BIG"
    assert derive_routing_decision(75.0, 80.0) == "CLOUD"
    assert derive_routing_decision(10.0, 30.0) == "CLOUD"  # red-alert CSS floor


def test_repeated_action_verbs_count_as_separate_deliverables() -> None:
    """Set-cardinality scored "refactor A, then refactor B, then refactor C" as ONE
    requirement, so a repetitive multi-target ask — the shape a small model is least
    able to hold in one draft — read as trivial and routed to the cheapest tier."""
    repeated = compute_task_complexity_index(
        user_input="refactor the auth file, then refactor the user file, "
                   "then refactor the db file",
        retrieved_files=1,
    )
    single = compute_task_complexity_index(
        user_input="refactor the auth file", retrieved_files=1
    )
    assert repeated > single
    assert derive_routing_decision(repeated, 80.0) != "LOCAL_SMALL"


def test_occurrence_counting_does_not_inflate_a_trivial_ask() -> None:
    """Counting occurrences must sharpen the signal, not raise the floor for
    everything — a one-verb edit stays on the cheap path."""
    assert derive_routing_decision(
        compute_task_complexity_index(user_input="fix the typo", retrieved_files=1), 80.0
    ) == "LOCAL_SMALL"


# ══════════════════════════════════════════════════════════════════════════════
# Semantic escalation must not contradict the score that justifies it
# ══════════════════════════════════════════════════════════════════════════════


def test_tier_floor_is_the_exact_inverse_of_the_bands() -> None:
    """Every floor must map back onto the tier it belongs to. Derived from the band
    constants, so retuning a band can never leave the two disagreeing."""
    for tier in ("LOCAL_SMALL", "LOCAL_MEDIUM", "LOCAL_BIG", "CLOUD"):
        assert derive_routing_decision(tci_floor_for_tier(tier), 80.0) == tier


def test_unknown_tier_never_inflates_the_score() -> None:
    """A floor that cannot be resolved must not silently raise a turn's complexity."""
    assert tci_floor_for_tier("NOT_A_TIER") == 0.0


@pytest.mark.parametrize("structural_tci", [10.0, 30.0, 35.0, 49.0, 55.0, 80.0])
def test_medium_verdict_keeps_score_and_decision_coherent(structural_tci: float) -> None:
    """The MEDIUM branch bumped TCI to a flat 75.0 — a CLOUD-band score — while
    routing to LOCAL_MEDIUM, so the persisted meter justified a tier the turn never
    ran on. The reviewable route card shows that score to the operator as the
    decision's rationale, so an incoherent pair is a defect the user can see."""
    math_routing = derive_routing_decision(structural_tci, 80.0)
    cascade = "LOCAL_BIG" if math_routing == "LOCAL_SMALL" else math_routing
    persisted = max(structural_tci, tci_floor_for_tier(cascade))
    assert derive_routing_decision(persisted, 80.0) == cascade


def test_medium_verdict_raises_to_the_floor_but_never_past_it() -> None:
    """The floor is a minimum, not an overwrite: a turn already scoring above its
    tier's floor keeps its own, more precise score."""
    assert max(60.0, tci_floor_for_tier("LOCAL_BIG")) == 60.0
    assert max(10.0, tci_floor_for_tier("LOCAL_BIG")) == 50.0


# ══════════════════════════════════════════════════════════════════════════════
# D1/D3 — planner tier floor, and a reachable CLOUD tier
# ══════════════════════════════════════════════════════════════════════════════


def test_planner_floor_lifts_small_but_never_lowers_anything() -> None:
    assert resolve_model_alias_for_routing(
        "LOCAL_SMALL", default=MODEL_BIG, floor="LOCAL_MEDIUM"
    ) == MODEL_MEDIUM
    for decision, expected in (
        ("LOCAL_MEDIUM", MODEL_MEDIUM), ("LOCAL_BIG", MODEL_BIG), ("CLOUD", MODEL_CLOUD),
    ):
        assert resolve_model_alias_for_routing(
            decision, default=MODEL_BIG, floor="LOCAL_MEDIUM"
        ) == expected


def test_coder_keeps_the_full_four_tier_range() -> None:
    """The asymmetry is deliberate: a plan's shape gates the whole turn, a coder
    step is per-file and has validate_output behind it."""
    assert resolve_model_alias_for_routing("LOCAL_SMALL", default=MODEL_BIG) == MODEL_SMALL


def test_cloud_decision_reaches_the_cloud_tier() -> None:
    """CLOUD mapped onto MODEL_BIG, so the top of the escalation ladder — where
    red-alert and HIGH-risk turns are sent — was unreachable from every agent."""
    assert resolve_model_alias_for_routing("CLOUD", default=MODEL_SMALL) == MODEL_CLOUD
    assert MODEL_CLOUD != MODEL_BIG


def test_unknown_decision_still_falls_back_to_the_caller_default() -> None:
    assert resolve_model_alias_for_routing(None, default=MODEL_BIG) == MODEL_BIG
    assert resolve_model_alias_for_routing("NONSENSE", default=MODEL_BIG) == MODEL_BIG
    assert resolve_model_alias_for_routing(
        None, default=MODEL_BIG, floor="LOCAL_MEDIUM"
    ) == MODEL_BIG


# ══════════════════════════════════════════════════════════════════════════════
# D10 — the Mini-Judge is the only semantic escalation gate
# ══════════════════════════════════════════════════════════════════════════════


async def test_judge_escalates_when_it_cannot_run() -> None:
    """It returned NONE on failure — "the classifier is down, therefore the task
    is trivial". A gate that cannot run must not assert the cheap answer."""
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke", new=AsyncMock(side_effect=RuntimeError("down"))
    ):
        assert await audit_task_complexity("refactor the auth module") is RiskLevel.MEDIUM


async def test_judge_escalates_one_notch_not_to_cloud() -> None:
    """A local-engine outage must not silently redirect every turn to a paid API."""
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke", new=AsyncMock(side_effect=RuntimeError("down"))
    ):
        verdict = await audit_task_complexity("refactor the auth module")
    assert verdict is not RiskLevel.HIGH


async def test_empty_input_is_still_genuinely_trivial() -> None:
    assert await audit_task_complexity("   ") is RiskLevel.NONE


def test_judge_runs_above_the_smallest_tier() -> None:
    """The model whose incompetence caused the incident must not also be the one
    deciding whether a stronger model is needed."""
    from shared.config import MINI_JUDGE_MODEL

    assert MINI_JUDGE_MODEL != MODEL_SMALL


# ══════════════════════════════════════════════════════════════════════════════
# D9 — turn-scoped ledgers leaked across turns
# ══════════════════════════════════════════════════════════════════════════════


def _payload(**kw: Any) -> TaskPayload:
    return TaskPayload(task_prompt=kw.pop("task_prompt", "do the thing"), dirty_buffers=[], **kw)


def test_turn_scoped_ledgers_start_empty_every_turn() -> None:
    """applied_files_log is an operator.add accumulator: a key omitted from the
    seed keeps its checkpointed value, so these carried across every turn —
    inflating the summary's counts and tripping the apply gate's cumulative
    blast-radius threshold on work the current turn never did.

    Asserts against the REAL seed builder, not a hand-written state dict: a
    hand-seeded fixture is exactly what hid this for so long.
    """
    state = TaskService()._build_initial_state("sess-ledger", _payload(), "balanced")
    for channel in ("applied_files_log", "applied_step_ids", "check_results", "errors"):
        assert state[channel] == [], f"{channel} must be reset per turn"


# ══════════════════════════════════════════════════════════════════════════════
# D8/§9 — executing the approved plan instead of re-drafting it
# ══════════════════════════════════════════════════════════════════════════════


def test_normal_turn_clears_any_checkpointed_plan() -> None:
    """Writing None CLOBBERS the channel — which is correct for a fresh request:
    a new instruction must never execute against a stale plan."""
    state = TaskService()._build_initial_state("sess-normal", _payload(), "balanced")
    assert state["mission_spec"] is None


def test_accepted_plan_omits_the_key_so_the_checkpoint_survives() -> None:
    """LangGraph channels are last-value: omitting the key preserves the plan,
    writing None destroys it. That distinction IS the mechanism."""
    svc = TaskService()
    with patch.object(TaskService, "_checkpoint_has_mission", staticmethod(lambda _s: True)):
        state = svc._build_initial_state(
            "sess-accept", _payload(accepted_plan=True), "balanced"
        )
    assert "mission_spec" not in state


def test_accepted_plan_fails_safe_when_no_plan_exists() -> None:
    """accepted_plan is client-supplied and therefore untrusted (§6.2): it must
    not let a turn skip planning on a thread that has no plan to execute."""
    svc = TaskService()
    with patch.object(TaskService, "_checkpoint_has_mission", staticmethod(lambda _s: False)):
        state = svc._build_initial_state(
            "sess-empty", _payload(accepted_plan=True), "balanced"
        )
    assert state["mission_spec"] is None


def test_accepted_plan_never_influences_the_permission_mode() -> None:
    """Approving a plan decides WHAT runs, never what it is allowed to do — the
    mode still comes from execution_mode alone."""
    from core.permissions import session_mode_from_frontend

    expected = session_mode_from_frontend("ask_before_edits")
    assert expected is not None
    svc = TaskService()
    with patch.object(TaskService, "_checkpoint_has_mission", staticmethod(lambda _s: True)):
        state = svc._build_initial_state(
            "sess-perm",
            _payload(accepted_plan=True, execution_mode="ask_before_edits"),
            "balanced",
        )
    assert state["session_permission_mode"] == expected.value.upper()


def test_checkpoint_probe_never_raises() -> None:
    with patch("brain.checkpoint.hybrid_checkpointer.get_tuple", side_effect=RuntimeError("boom")):
        assert TaskService._checkpoint_has_mission("sess-broken") is False


def test_router_executes_a_carried_plan_before_considering_planning() -> None:
    from brain.engine import route_after_summarize

    carried = {"task_id": "t", "mission_spec": object(), "planner_mode_active": True}
    assert route_after_summarize(carried) == "step_dispatch"
    assert route_after_summarize({"task_id": "t", "planner_mode_active": True}) == "ideation_loop"
    assert route_after_summarize({"task_id": "t"}) == "planner_agent"


# ══════════════════════════════════════════════════════════════════════════════
# D11 — the DriftMonitor deletion left the step-advance loop intact
# ══════════════════════════════════════════════════════════════════════════════


def test_graph_still_advances_across_wbs_steps_without_the_drift_nodes() -> None:
    from brain.engine import alienant_app

    graph = alienant_app.get_graph()
    node_ids = set(graph.nodes)
    assert "step_dispatch" in node_ids
    assert not [n for n in node_ids if "drift" in n]
    # step_dispatch is the fan-out anchor: validate_output loops back through it
    # to advance to the next step, so severing it would strand multi-step plans.
    edges = {(e.source, e.target) for e in graph.edges}
    assert any(s == "step_dispatch" for s, _ in edges)
    assert any(t == "step_dispatch" for _, t in edges)


def test_removed_channels_are_gone_from_the_state_contract() -> None:
    from brain.state import AIlienantGraphState

    keys = set(AIlienantGraphState.__annotations__)
    assert {"immutable_wbs", "drift_gate_open", "drift_similarity"}.isdisjoint(keys)


# ══════════════════════════════════════════════════════════════════════════════
# D12 — the grill and the synthesis assembled context with retrieval off
# ══════════════════════════════════════════════════════════════════════════════


def _capture_assembler() -> Tuple[Dict[str, Any], Any]:
    seen: Dict[str, Any] = {}

    async def _fake_assemble(
        paths: List[str], project_id: Optional[str], session_id: str, *a: Any, **kw: Any,
    ) -> str:
        seen.update(kw)
        return "block"

    return seen, _fake_assemble


def test_socratic_grill_passes_its_retrieval_through() -> None:
    """The assembler retrieves nothing itself, so omitting the snippets silently
    emptied the GraphRAG layer of the block the grill reasons over."""
    from agents.analyst import _assemble_socratic_context

    seen, fake = _capture_assembler()
    with patch("agents.analyst_context.assemble_analyst_context", new=fake), patch(
        "agents.analyst_context.fetch_intent_snippets",
        new=AsyncMock(return_value=[("a.py", "def a(): ...")]),
    ):
        asyncio.run(_assemble_socratic_context(
            {"workspace_root": "/ws", "project_id": "p", "task_id": "t", "user_input": "q"}
        ))
    assert seen.get("rag_snippets") == [("a.py", "def a(): ...")]


def test_ideation_synthesis_passes_its_retrieval_through() -> None:
    """Highest leverage of the two: this block becomes the planner's brief."""
    from brain.ideation import _assemble_synthesis_context

    seen, fake = _capture_assembler()
    with patch("agents.analyst_context.assemble_analyst_context", new=fake), patch(
        "agents.analyst_context.fetch_intent_snippets",
        new=AsyncMock(return_value=[("b.py", "def b(): ...")]),
    ):
        asyncio.run(_assemble_synthesis_context(
            {"workspace_root": "/ws", "project_id": "p", "task_id": "t", "user_input": "q"}
        ))
    assert seen.get("rag_snippets") == [("b.py", "def b(): ...")]
