# ailienant-core/brain/dispatch_ledger.py
"""Budget admission for dynamic subagent dispatch — reserve / commit / refund.

A dispatch wave consumes the parent task's budget, which the graph already tracks in the
authoritative ``current_cost_usd`` (``operator.add``) and ``max_budget_usd`` (scalar) state
channels — the same pair ``brain/finops.py`` gates on. This module is therefore *not* a
second, file-backed ledger (which would double-book the same spend and desync from the
checkpoint): it is a set of pure functions that reuse only ``gateway/ledger.py``'s
floor-at-zero-refund discipline and the reserve→commit→refund sequencing (Charter §5.4).
The caller folds each returned delta into ``current_cost_usd`` via the existing reducer.

Because a reservation is single-flight at a wave boundary (one node reserves before fan-out,
one reconciles at synthesis), there is no concurrent mutation to lock, so the functions are
plain synchronous arithmetic.

Cost is an estimate: the reserve-time projection under-models output tokens and per-iteration
context growth, so admission is deliberately *lenient* — the hard ceiling remains
``finops``/``check_governor`` (DEBT-105).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("DISPATCH_LEDGER")


@dataclass(frozen=True)
class DispatchReservation:
    """An accepted budget reservation for one dispatch wave.

    ``task_id`` (parent graph task) and ``batch_id`` (this dispatch) are carried for
    traceability/logging; only ``reserved_usd`` drives the reconciliation arithmetic.
    """

    task_id: str
    batch_id: str
    reserved_usd: float


def estimate_task_cost(task: Mapping[str, Any]) -> float:
    """Worst-case cost estimate for one ``SubagentTask`` (as a plain mapping).

    Projects ``max_iterations`` turns, each roughly the size of the worker's seed prompt
    (``description`` + the response-schema shape), via the one billing formula in
    ``estimate_iteration_cost``. Outputs are unknown at reserve time, so this under-models
    completion tokens and context growth — admission is lenient by design (DEBT-105).
    """
    from brain.iteration_governor import estimate_iteration_cost

    description = str(task.get("description", ""))
    schema = task.get("response_schema") or {}
    fields = schema.get("fields", []) if isinstance(schema, Mapping) else []
    field_shape = " ".join(
        f"{f.get('name', '')}:{f.get('type', '')}"
        for f in fields
        if isinstance(f, Mapping)
    )
    seed = (
        f"You are the '{task.get('subagent_role', '')}' subagent. Task:\n{description}\n\n"
        f"Return a structured result with fields: {field_shape}"
    )
    max_iters = max(1, int(task.get("max_iterations", 1) or 1))
    per_iteration = estimate_iteration_cost([{"content": seed}], [])
    return per_iteration * max_iters


def estimate_wave_cost(tasks: Sequence[Mapping[str, Any]]) -> float:
    """Sum of per-task worst-case estimates across a wave's tasks."""
    return sum(estimate_task_cost(task) for task in tasks)


def reserve_dispatch_budget(
    *,
    task_id: str,
    batch_id: str,
    estimated_cost_usd: float,
    current_cost_usd: float,
    max_budget_usd: float,
) -> Optional[DispatchReservation]:
    """Fail-closed admission: reserve ``estimated_cost_usd`` against the remaining budget.

    Returns ``None`` (deny) when booking the estimate would exceed ``max_budget_usd``; the
    ``==`` boundary is admitted. A negative estimate is coerced to zero. The caller books an
    accepted reservation by folding ``{"current_cost_usd": reserved_usd}`` into state.
    """
    estimate = max(0.0, estimated_cost_usd)
    if current_cost_usd + estimate > max_budget_usd:
        logger.info(
            "dispatch budget DENIED for batch=%s (task=%s): current=%.6f + est=%.6f > max=%.6f",
            batch_id, task_id, current_cost_usd, estimate, max_budget_usd,
        )
        return None
    return DispatchReservation(task_id=task_id, batch_id=batch_id, reserved_usd=estimate)


def commit_dispatch_actual(
    reservation: DispatchReservation, actual_cost_usd: float
) -> float:
    """Reconcile actual spend against a reservation; return the refund delta (``>= 0``).

    The refund is ``reserved_usd - actual``, floored at zero (an actual that overran the
    estimate refunds nothing — never a negative refund) and clamped to ``reserved_usd`` so a
    refund can never gift budget headroom beyond what was reserved. The caller applies it by
    folding ``{"current_cost_usd": -refund}`` into state.
    """
    actual = max(0.0, actual_cost_usd)
    refund = reservation.reserved_usd - actual
    if refund <= 0.0:
        return 0.0
    return min(refund, reservation.reserved_usd)


def refund_dispatch_reservation(reservation: DispatchReservation) -> float:
    """Full-refund compensation for a wave that reserved but never ran (total failure).

    Returns the entire ``reserved_usd`` (equivalently ``commit_dispatch_actual`` with an
    actual of zero). The caller folds ``{"current_cost_usd": -reserved_usd}`` into state.
    """
    return max(0.0, reservation.reserved_usd)
