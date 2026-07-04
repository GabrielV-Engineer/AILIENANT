# ailienant-core/tests/test_dispatch_ledger.py
"""8.15.4 DoD — dispatch budget-admission ledger (reserve / commit / refund).

Pure-function unit tests over the state-channel ledger: fail-closed admission denies an
over-cap wave, reconciliation refunds the unused reservation, a total failure refunds in
full, and floor-at-zero guarantees a refund can never gift budget headroom beyond what was
reserved. The per-task/per-wave estimator is exercised for monotonicity, not exact dollars.
"""
from __future__ import annotations

from typing import Any, Dict

from brain.dispatch_ledger import (
    DispatchReservation,
    commit_dispatch_actual,
    estimate_task_cost,
    estimate_wave_cost,
    refund_dispatch_reservation,
    reserve_dispatch_budget,
)


def _task(desc: str = "do the work", iters: int = 1) -> Dict[str, Any]:
    return {
        "task_id": "t0",
        "description": desc,
        "subagent_role": "core_dev",
        "response_schema": {"fields": [{"name": "summary", "type": "str", "description": "x"}]},
        "max_iterations": iters,
    }


def _reservation(reserved: float) -> DispatchReservation:
    return DispatchReservation(task_id="t0", batch_id="b0", reserved_usd=reserved)


# ── reserve: fail-closed admission ──────────────────────────────────────────────


def test_reserve_within_budget_returns_reservation() -> None:
    res = reserve_dispatch_budget(
        task_id="t0", batch_id="b0",
        estimated_cost_usd=2.0, current_cost_usd=1.0, max_budget_usd=10.0,
    )
    assert res is not None
    assert res.reserved_usd == 2.0
    assert (res.task_id, res.batch_id) == ("t0", "b0")


def test_reserve_at_exact_ceiling_is_admitted() -> None:
    # current + est == max is allowed (only a strict overrun denies).
    res = reserve_dispatch_budget(
        task_id="t0", batch_id="b0",
        estimated_cost_usd=4.0, current_cost_usd=6.0, max_budget_usd=10.0,
    )
    assert res is not None


def test_reserve_over_cap_denies() -> None:
    res = reserve_dispatch_budget(
        task_id="t0", batch_id="b0",
        estimated_cost_usd=5.0, current_cost_usd=6.0, max_budget_usd=10.0,
    )
    assert res is None


def test_reserve_already_over_budget_denies_any_positive_estimate() -> None:
    res = reserve_dispatch_budget(
        task_id="t0", batch_id="b0",
        estimated_cost_usd=0.01, current_cost_usd=11.0, max_budget_usd=10.0,
    )
    assert res is None


def test_reserve_negative_estimate_coerced_to_zero() -> None:
    res = reserve_dispatch_budget(
        task_id="t0", batch_id="b0",
        estimated_cost_usd=-5.0, current_cost_usd=9.0, max_budget_usd=10.0,
    )
    assert res is not None
    assert res.reserved_usd == 0.0


# ── estimator: monotonic, non-negative ──────────────────────────────────────────


def test_estimate_task_cost_scales_with_iterations() -> None:
    one = estimate_task_cost(_task(iters=1))
    four = estimate_task_cost(_task(iters=4))
    assert one > 0.0
    assert four > one  # more iterations → strictly higher worst-case


def test_estimate_wave_cost_sums_per_task() -> None:
    tasks = [_task(iters=1), _task(iters=1), _task(iters=1)]
    wave = estimate_wave_cost(tasks)
    assert wave == estimate_task_cost(_task(iters=1)) * 3


def test_estimate_wave_cost_empty_is_zero() -> None:
    assert estimate_wave_cost([]) == 0.0


# ── commit: reconcile, floor-at-zero, never gift headroom ───────────────────────


def test_commit_refunds_unused_reservation() -> None:
    assert commit_dispatch_actual(_reservation(5.0), 2.0) == 3.0


def test_commit_exact_actual_refunds_zero() -> None:
    assert commit_dispatch_actual(_reservation(5.0), 5.0) == 0.0


def test_commit_overrun_refunds_zero_never_negative() -> None:
    # actual exceeded the estimate → no refund, and never a negative delta.
    assert commit_dispatch_actual(_reservation(5.0), 8.0) == 0.0


def test_commit_negative_actual_coerced() -> None:
    # A malformed negative actual cannot manufacture a refund beyond the reservation.
    assert commit_dispatch_actual(_reservation(5.0), -3.0) == 5.0


def test_commit_refund_never_exceeds_reservation() -> None:
    refund = commit_dispatch_actual(_reservation(5.0), 0.0)
    assert refund == 5.0  # full reservation back, but no more (no gifted headroom)


# ── refund: total-failure compensation ──────────────────────────────────────────


def test_full_refund_returns_entire_reservation() -> None:
    assert refund_dispatch_reservation(_reservation(7.5)) == 7.5


def test_full_refund_of_zero_reservation_is_zero() -> None:
    assert refund_dispatch_reservation(_reservation(0.0)) == 0.0
