# tests/test_phase8_15_checkpoint_gate.py
"""Dynamic Subagent Dispatch — Division Checkpoint Gate.

Test-only certification that Division 8.15's cross-cutting invariants hold together
against their shipped entry points. It imports and invokes production code
(``brain.dispatch``, ``brain.dispatch_ledger``, ``brain.nodes.dispatch_synthesize_node``,
``core.permissions``, ``shared.rbac``, ``brain.agentic_cell`` / ``brain.subagent_tournament``),
asserting one load-bearing invariant per row; it modifies no production logic and follows
the sibling-gate convention. Sub-phase unit tests already cover each piece in isolation
(``test_dispatch_synthesis.py``, ``test_dispatch_ledger.py``, ``test_subagent_tournament.py``,
``test_dispatch_wiring.py``); this gate re-certifies the guarantees that must hold *together*
and never re-runs a sibling suite. The one async row drives an inner ``_run()`` via
``asyncio.run`` (no pytest-asyncio), mirroring the house style.

Rows certified here:
  RELOC1    the 8.15.3 tournament extraction left agentic_cell's callers intact — the
            ``select_candidate_via_mcts`` re-export shim + single-sourced ``_content_to_vfs``
  RELOC2    the 8.15.1 worker extraction reuses the shared ToolDispatcher engine (dispatch
            seam on ``ToolDispatcher.__init__``) rather than forking the tool loop
  DEPTH1    the depth ceiling DENIES (never truncates): at-ceiling state + malformed
            over-depth raw plan both yield ``status="denied"`` and no fan-out
  WIDTH1    the width ceiling denies AND rejection-envelope generation is OOM-bounded to
            ``MAX_DISPATCH_WIDTH`` (never one-per-raw-task)
  BUDGET1   an over-cap reserve denies (fail-closed) and short-circuits to synthesis with
            ``budget_exhausted`` envelopes; the ``==`` boundary is admitted
  BUDGET2   refund reconciliation: partial spend → positive refund, overrun → zero,
            total failure → full refund (floored + clamped)
  FLOOR1    ``analyst_readonly`` cannot reach a WRITE/EXECUTE/DANGEROUS tool under ANY
            session mode (the automatic READ_ONLY floor-lock)
  DIGEST1   the batch digest never exceeds the parent's context-window tier ceiling and
            scales with ``active_llm_profile.context_window`` (async)
  DESERIAL1 a pre-8.15 checkpoint (none of the 12 dispatch channels present) drives every
            reader to its safe default with no ``KeyError``
  FANOUT1   the named product ceiling is internally consistent
            (``MAX_DISPATCH_DEPTH * MAX_DISPATCH_WIDTH == MAX_TOTAL_DISPATCH_FANOUT``) and
            matches the Pydantic fan-out/recursion bounds
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

import brain.agentic_cell as ac
import brain.dispatch_ledger as ledger
import brain.subagent_tournament as st
from brain import dispatch as dispatch_mod
from brain.dispatch import (
    DISPATCH_FANOUT_NODE,
    DISPATCH_SYNTHESIZE_NODE,
    build_dispatch_sends,
    dispatch_origin,
    route_after_admission,
    route_after_synthesis,
    route_after_workers,
    _denied_envelopes,
    _DEFAULT_RETURN_NODE,
)
from brain.dispatch_ledger import (
    DispatchReservation,
    commit_dispatch_actual,
    refund_dispatch_reservation,
    reserve_dispatch_budget,
)
from brain.nodes.dispatch_synthesize_node import (
    _CHARS_PER_TOKEN,
    _DIGEST_BUDGET_FRAC,
    dispatch_synthesize,
)
from brain.subagent_contracts import (
    DispatchPlan,
    SubagentResultEnvelope,
)
from brain.subagent_tournament import run_tournament, run_tournament_from_dispatch
from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
)
from core.tool_dispatch import ToolDispatcher
from shared.config import (
    MAX_DISPATCH_DEPTH,
    MAX_DISPATCH_ROUNDS,
    MAX_DISPATCH_WIDTH,
    MAX_TOTAL_DISPATCH_FANOUT,
)
from shared.rbac import PermissionMode, resolve_dispatch_permission


# ── helpers ───────────────────────────────────────────────────────────────────


# The complete set of dispatch state channels landed across §30 (4) + §31 (2) +
# §32 (6). A pre-8.15 checkpoint has NONE of these keys; every reader must resolve
# each to its documented safe default via ``state.get`` / reducer-empty — the
# TypedDict itself supplies no defaults.
_ALL_DISPATCH_CHANNELS = (
    # §30
    "dispatch_plan",
    "dispatch_batch_result",
    "dispatch_depth",
    "subagent_dispatch_trace",
    # §31
    "_dispatch_results",
    "dispatch_wave_count",
    # §32
    "dispatch_return_node",
    "dispatch_round_count",
    "_dispatch_consumed",
    "_dispatch_reserved_usd",
    "_dispatch_reserved_round",
    "_dispatch_admission",
)


def _task(task_id: str = "t0", role: str = "core_dev") -> Dict[str, Any]:
    """A minimal valid ``SubagentTask`` mapping (as stored in ``dispatch_plan``)."""
    return {
        "task_id": task_id,
        "description": "do a bounded unit of work",
        "subagent_role": role,
        "response_schema": {
            "fields": [{"name": "result", "type": "str", "description": "the answer"}]
        },
        "context_refs": [],
        "max_iterations": 1,
    }


def _plan(
    tasks: List[Dict[str, Any]],
    *,
    pattern: str = "fanout_and_synthesize",
    dispatch_depth: int = 0,
) -> Dict[str, Any]:
    """A ``DispatchPlan`` mapping (the ``model_dump()`` shape held in state)."""
    return {
        "pattern": pattern,
        "tasks": tasks,
        "synthesis_instruction": "combine the results",
        "dispatch_depth": dispatch_depth,
    }


def _ok_envelope(task_id: str, digest_len: int) -> Dict[str, Any]:
    """A valid ``status="ok"`` result envelope with a digest of the given length."""
    return SubagentResultEnvelope(
        task_id=task_id, status="ok", raw_digest="x" * digest_len
    ).model_dump()


# ── RELOC1 ────────────────────────────────────────────────────────────────────


def test_reloc1_tournament_extraction_keeps_callers_intact() -> None:
    """8.15.3: the relocation left agentic_cell's public surface byte-identical.

    ``select_candidate_via_mcts`` is the re-export of ``run_tournament`` and
    ``_content_to_vfs`` is single-sourced from the new home — so the swarm / agentic
    cell callers keep resolving the same object (regression proof for extraction #1).
    """
    assert ac.select_candidate_via_mcts is run_tournament
    assert callable(ac.select_candidate_via_mcts)
    assert ac._content_to_vfs is st._content_to_vfs
    # The dispatch adapter over the same engine exists and is callable.
    assert callable(run_tournament_from_dispatch)


# ── RELOC2 ────────────────────────────────────────────────────────────────────


def test_reloc2_worker_reuses_shared_tool_dispatcher_engine() -> None:
    """8.15.1: the worker delegates to the ONE shared ToolDispatcher, not a fork.

    The dispatch integration lives on the shared engine's constructor (the per-role
    ``agent_permission`` / ``active_role`` seam ``subagent_worker`` passes through), so
    there is no second tool-loop implementation to drift from the RELAY/SWARM path —
    the invariant that kept ``test_swarms.py`` green through extraction #2.
    """
    params = set(inspect.signature(ToolDispatcher.__init__).parameters)
    assert {"active_role", "session_mode", "state", "agent_permission"} <= params


# ── DEPTH1 ────────────────────────────────────────────────────────────────────


def test_depth1_ceiling_denies_never_truncates() -> None:
    """A depth at the recursion ceiling denies the whole wave — it is never truncated."""
    plan = _plan([_task("a"), _task("b")])

    # (1) State already at the depth ceiling → admission denies every task, no fan-out.
    at_ceiling = {"dispatch_plan": plan, "dispatch_depth": MAX_DISPATCH_DEPTH}
    verdict = dispatch_origin(at_ceiling)
    assert verdict["_dispatch_admission"] == "denied"
    envelopes = verdict["_dispatch_results"]
    assert len(envelopes) == len(plan["tasks"])
    assert all(e["status"] == "denied" for e in envelopes)
    assert route_after_admission({**at_ceiling, **verdict}) == DISPATCH_SYNTHESIZE_NODE

    # (2) The defense-in-depth inline guard emits no Sends at the ceiling.
    typed_plan = DispatchPlan.model_validate(plan)
    assert build_dispatch_sends(typed_plan, {"dispatch_depth": MAX_DISPATCH_DEPTH}) == []

    # (3) A raw plan claiming an over-ceiling depth never even constructs (Pydantic
    #     rejects le=2), and the admission gate turns that into a denial, not a crash.
    with pytest.raises(ValidationError):
        DispatchPlan.model_validate(_plan([_task("a")], dispatch_depth=MAX_DISPATCH_DEPTH + 1))
    over_depth = {"dispatch_plan": _plan([_task("a")], dispatch_depth=MAX_DISPATCH_DEPTH + 1)}
    denied = dispatch_origin(over_depth)
    assert denied["_dispatch_admission"] == "denied"
    assert all(e["status"] == "denied" for e in denied["_dispatch_results"])


# ── WIDTH1 ────────────────────────────────────────────────────────────────────


def test_width1_ceiling_denies_and_rejection_is_oom_bounded() -> None:
    """An over-width plan is denied, and rejection-envelope generation is width-bounded."""
    over_width_tasks = [_task(f"t{i}") for i in range(MAX_DISPATCH_WIDTH + 8)]
    # A plan wider than the Pydantic cap raises → the gate reports denial envelopes,
    # bounded to the width ceiling rather than one-per-raw-task.
    verdict = dispatch_origin({"dispatch_plan": _plan(over_width_tasks)})
    assert verdict["_dispatch_admission"] == "denied"
    envelopes = verdict["_dispatch_results"]
    assert len(envelopes) == MAX_DISPATCH_WIDTH
    assert all(e["status"] == "denied" for e in envelopes)

    # The OOM guard proper: even a pathological raw list never materializes beyond the
    # ceiling (asserted directly against the envelope builder — no Pydantic cost).
    huge = [{"task_id": str(i)} for i in range(5000)]
    assert len(_denied_envelopes(huge, "denied", "x")) == MAX_DISPATCH_WIDTH


# ── BUDGET1 ───────────────────────────────────────────────────────────────────


def test_budget1_over_cap_reserve_denies_without_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed budget admission: an over-cap wave denies and never fans out."""
    # Direct ledger contract: strict-``>`` deny, ``==`` boundary admit.
    assert (
        reserve_dispatch_budget(
            task_id="t", batch_id="b",
            estimated_cost_usd=10.0, current_cost_usd=5.0, max_budget_usd=10.0,
        )
        is None
    )
    boundary = reserve_dispatch_budget(
        task_id="t", batch_id="b",
        estimated_cost_usd=5.0, current_cost_usd=5.0, max_budget_usd=10.0,
    )
    assert boundary is not None and boundary.reserved_usd == 5.0

    # Wiring: dispatch_origin reserves single-flight and, on deny, short-circuits to
    # synthesis with budget_exhausted envelopes. Estimate is pinned high so the deny is
    # deterministic regardless of the real cost model.
    monkeypatch.setattr(ledger, "estimate_wave_cost", lambda tasks: 1_000_000.0)
    state = {
        "dispatch_plan": _plan([_task("a"), _task("b")]),
        "dispatch_depth": 0,
        "dispatch_wave_count": 0,
        "dispatch_round_count": 0,
        "current_cost_usd": 0.0,
        "max_budget_usd": 0.01,
    }
    verdict = dispatch_origin(state)
    assert verdict["_dispatch_admission"] == "budget_exhausted"
    assert all(e["status"] == "budget_exhausted" for e in verdict["_dispatch_results"])
    assert route_after_admission({**state, **verdict}) == DISPATCH_SYNTHESIZE_NODE


# ── BUDGET2 ───────────────────────────────────────────────────────────────────


def test_budget2_refund_reconciles_partial_total_and_overrun() -> None:
    """Refund is ``clamp(reserved - actual, 0, reserved)``; total failure refunds all."""
    reservation = DispatchReservation(task_id="t", batch_id="b", reserved_usd=1.0)

    # Partial spend → positive refund of the unused delta.
    assert commit_dispatch_actual(reservation, 0.4) == pytest.approx(0.6)
    # Overrun → zero refund (never negative — the estimate simply under-booked).
    assert commit_dispatch_actual(reservation, 1.5) == 0.0
    # A negative actual is coerced to zero, and the refund is clamped to the reservation.
    assert commit_dispatch_actual(reservation, -5.0) == pytest.approx(1.0)
    # Total failure (reserved, never ran) → full refund.
    assert refund_dispatch_reservation(reservation) == pytest.approx(1.0)


# ── FLOOR1 ────────────────────────────────────────────────────────────────────


def test_floor1_analyst_readonly_locked_under_every_session_mode() -> None:
    """analyst_readonly cannot reach a WRITE/EXECUTE/DANGEROUS tool in any session mode."""
    assert resolve_dispatch_permission("analyst_readonly") is PermissionMode.READ_ONLY
    # Dev roles carry the write/execute-capable identity; unknown roles fail safe.
    assert resolve_dispatch_permission("core_dev") is PermissionMode.EDIT_EXECUTE_RBW
    assert resolve_dispatch_permission("no_such_role") is PermissionMode.READ_ONLY

    privileged = (
        ToolPrivilegeTier.WRITE,
        ToolPrivilegeTier.EXECUTE,
        ToolPrivilegeTier.DANGEROUS,
    )
    for mode in SessionPermissionMode:  # all 10 (7 canonical + 3 legacy aliases)
        for tier in privileged:
            assert (
                evaluate_action(mode, tier, PermissionMode.READ_ONLY)
                is PermissionDecision.DENY
            ), f"floor-lock breached for mode={mode} tier={tier}"
        # The floor-lock is surgical: a READ_ONLY tier is NOT blanket-denied — it is
        # handed to the (mode, tier) matrix like any other read.
        assert (
            evaluate_action(mode, ToolPrivilegeTier.READ_ONLY, PermissionMode.READ_ONLY)
            is not PermissionDecision.DENY
            or mode in (SessionPermissionMode.PLAN_ONLY, SessionPermissionMode.PLAN)
        )


# ── DIGEST1 ───────────────────────────────────────────────────────────────────


def test_digest1_batch_digest_bounded_by_parent_context_window() -> None:
    """The synthesized digest never exceeds the parent tier ceiling and scales with it."""
    envelopes = [_ok_envelope(f"t{i}", digest_len=120) for i in range(6)]

    async def _run() -> None:
        # Small window: ceiling = 200 * 4.0 * 0.5 = 400 chars → trims the fan-out.
        small_state = {
            "_dispatch_results": envelopes,
            "_dispatch_consumed": 0,
            "dispatch_plan": _plan([_task("a")]),
            "active_llm_profile": SimpleNamespace(context_window=200),
        }
        small = await dispatch_synthesize(small_state)
        small_results = small["dispatch_batch_result"]["results"]
        small_chars = sum(len(r["raw_digest"]) for r in small_results)
        small_ceiling = int(200 * _CHARS_PER_TOKEN * _DIGEST_BUDGET_FRAC)
        assert small_chars <= small_ceiling
        assert len(small_results) < len(envelopes)  # trimming actually happened

        # Large window: ceiling scales up → strictly more envelopes survive the pack.
        large_state = {
            "_dispatch_results": envelopes,
            "_dispatch_consumed": 0,
            "dispatch_plan": _plan([_task("a")]),
            "active_llm_profile": SimpleNamespace(context_window=2000),
        }
        large = await dispatch_synthesize(large_state)
        large_results = large["dispatch_batch_result"]["results"]
        assert len(large_results) >= len(small_results)
        assert len(large_results) == len(envelopes)  # everything fits under 4000 chars

    asyncio.run(_run())


# ── DESERIAL1 ─────────────────────────────────────────────────────────────────


def test_deserial1_pre_8_15_checkpoint_resolves_safe_defaults() -> None:
    """A checkpoint predating 8.15 has none of the 12 channels; readers must not crash."""
    legacy_state: Dict[str, Any] = {"task_id": "legacy-task"}  # a pre-dispatch checkpoint
    for channel in _ALL_DISPATCH_CHANNELS:
        assert channel not in legacy_state

    # Return router → the documented drift_compute fallback (channel absent → None → default).
    assert route_after_synthesis(legacy_state) == _DEFAULT_RETURN_NODE == "drift_compute"

    # Fan-in router → no waves, no round, no pattern ⇒ proceed to synthesis (no KeyError).
    assert route_after_workers(legacy_state) == DISPATCH_SYNTHESIZE_NODE

    # Admission gate tolerates a channel-less state (empty plan ⇒ denial, never a crash).
    verdict = dispatch_origin(legacy_state)
    assert verdict["_dispatch_admission"] in {"denied", "budget_exhausted", "admit"}

    # dispatch_depth absent ⇒ read as 0 ⇒ a valid plan fans out (proves the .get default,
    # not a KeyError). One task ⇒ one Send.
    typed_plan = DispatchPlan.model_validate(_plan([_task("only")]))
    sends = build_dispatch_sends(typed_plan, legacy_state)
    assert len(sends) == 1


# ── FANOUT1 ───────────────────────────────────────────────────────────────────


def test_fanout1_product_ceiling_is_internally_consistent() -> None:
    """The named total-fanout ceiling equals depth × width and matches the Pydantic bounds."""
    assert MAX_DISPATCH_DEPTH * MAX_DISPATCH_WIDTH == MAX_TOTAL_DISPATCH_FANOUT == 64
    assert MAX_DISPATCH_DEPTH >= 1
    assert MAX_DISPATCH_WIDTH >= 1
    assert MAX_DISPATCH_ROUNDS >= 1

    # The config width/depth ceilings are the SAME numbers the contract enforces at
    # construction — a plan exactly at the ceiling validates, one past it does not.
    at_width = _plan([_task(f"t{i}") for i in range(MAX_DISPATCH_WIDTH)])
    assert DispatchPlan.model_validate(at_width).tasks  # exactly 32 ⇒ valid
    with pytest.raises(ValidationError):
        DispatchPlan.model_validate(_plan([_task(f"t{i}") for i in range(MAX_DISPATCH_WIDTH + 1)]))

    assert DispatchPlan.model_validate(_plan([_task("a")], dispatch_depth=MAX_DISPATCH_DEPTH))
    with pytest.raises(ValidationError):
        DispatchPlan.model_validate(_plan([_task("a")], dispatch_depth=MAX_DISPATCH_DEPTH + 1))
