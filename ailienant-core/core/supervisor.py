"""Phase 6.5 — FinOps Cost Circuit Breaker & Graph Health Monitor.

``run_supervisor_node`` is a **deterministic, token-free** LangGraph node spliced
between ``finops_gate`` and ``apply_patch``. It never calls an LLM. Every pass it:

1. **Audit chain verify** — compares ``state["hitl_audit_chain_head"]`` against
   the DB head (``core/audit.py``); divergence raises :class:`AuditChainBrokenError`.
2. **Ledger → state sync** — reads ``token_ledger.snapshot()`` and publishes the
   accumulated cost to ``state["accumulated_session_cost"]``. This closes the
   historic decoupling bug: ``token_ledger`` is process-global, while
   ``state["current_cost_usd"]`` only aggregates per-fan-out within one graph
   invocation and resets between tasks.
3. **Hard kill** — above 1.10× of the session budget: flags the run, forces a
   DLQ episode for continuity, and routes to ``END``.
4. **Soft HITL gate** — above 1.00×: asks the human; approval doubles the
   ceiling, denial/timeout falls through to hard-kill mechanics.
5. **Token-spike trip** — a single-turn token delta above
   ``AILIENANT_MAX_TOKENS_PER_TURN`` raises an advisory HITL prompt.

Blueprint reference: §6 (FinOps Supervisor & Graph Health Monitor).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, cast

from brain.state import AIlienantGraphState
from core.audit import AuditChainBrokenError, get_chain_head
from core.token_ledger import token_ledger

logger = logging.getLogger("SUPERVISOR")

# Multiplier above the session budget at which the run is hard-killed (no
# recovery, DLQ written for continuity). 1.00× is the soft HITL gate.
_HARD_KILL_MULTIPLIER: float = 1.10

# Sentinel appended to state["security_flags"] on a terminal pass. Read by
# route_after_supervisor to send the graph to END. security_flags is an
# additive channel (Annotated[List[str], operator.add]); the sentinel is only
# ever appended when the run is about to terminate, so it never lingers.
_HARD_KILL_FLAG: str = "SESSION_BUDGET_HARD_KILL"

# Per-task cumulative token total seen on the previous Supervisor pass. The
# ledger is process-global and cumulative with no per-turn marker, so the
# single-turn delta is reconstructed here. Keyed by task_id; deterministic.
_LAST_TURN_TOKENS: Dict[str, float] = {}


def _default_budget() -> float:
    """Session budget ceiling fallback when state carries no usable value."""
    try:
        return float(os.getenv("AILIENANT_MAX_SESSION_BUDGET_USD", "5.00"))
    except ValueError:
        return 5.00


def _max_tokens_per_turn() -> int:
    """Single-turn token ceiling above which a TOKEN_SPIKE HITL is raised."""
    try:
        return int(os.getenv("AILIENANT_MAX_TOKENS_PER_TURN", "64000"))
    except ValueError:
        return 64000


def _format_budget_breach(
    session_cost: float, budget: float, snap: Dict[str, float]
) -> str:
    """Human-readable HITL payload for a BUDGET_OVERFLOW approval request."""
    return (
        f"Session spend ${session_cost:.4f} USD exceeded the ceiling "
        f"${budget:.4f} USD. Ledger: local={snap['local_tokens']:.0f} tok, "
        f"cloud={snap['cloud_tokens']:.0f} tok, "
        f"invested=${snap['estimated_invested_usd']:.4f} USD. "
        f"Approve to double the ceiling and continue?"
    )


async def _force_dlq(
    state: AIlienantGraphState, task_id: str, *, reason: str, detail: str
) -> None:
    """Persist a DLQ episode so a budget-halted run stays resumable.

    Best-effort: a DLQ write failure is logged, never raised — the hard-kill
    routing must still proceed.
    """
    from core.dead_letter import save_dead_letter

    exc = RuntimeError(f"{_HARD_KILL_FLAG} ({reason}): {detail}")
    try:
        await save_dead_letter(
            task_id=task_id,
            thread_id=task_id,
            failed_node="supervisor_node",
            exc=exc,
            state=cast(Dict[str, Any], state),
        )
    except Exception as dlq_exc:  # noqa: BLE001 — best-effort continuity
        logger.error("Supervisor: DLQ write failed for task=%s: %s", task_id, dlq_exc)


async def run_supervisor_node(state: AIlienantGraphState) -> Dict[str, Any]:
    """Deterministic FinOps + graph-health gate. Zero LLM calls, zero tokens.

    Returns a state patch. ``security_flags`` carrying ``SESSION_BUDGET_HARD_KILL``
    instructs :func:`route_after_supervisor` to terminate the graph.
    """
    task_id: str = state.get("task_id", "") or ""

    # === Trigger 1 — Audit chain verify (cheapest, hardest fail) ===========
    db_head = await get_chain_head(task_id)
    state_head = state.get("hitl_audit_chain_head")
    if state_head is not None and state_head != db_head:
        logger.error(
            "Supervisor: audit chain divergence for task=%s "
            "(state_head=%r, db_head=%r).",
            task_id, state_head, db_head,
        )
        raise AuditChainBrokenError(
            state_head=state_head, db_head=db_head, task_id=task_id
        )

    # === Trigger 2 — Ledger → state sync ==================================
    snap: Dict[str, float] = token_ledger.snapshot()
    session_cost: float = snap["estimated_invested_usd"]
    patch: Dict[str, Any] = {"accumulated_session_cost": session_cost}

    budget: float = state.get("session_max_budget_usd") or _default_budget()

    # === Trigger 3 — Hard kill (no recovery; DLQ written for continuity) ===
    if session_cost > budget * _HARD_KILL_MULTIPLIER:
        logger.error(
            "Supervisor HARD KILL: cost=$%.4f > %.2fx ceiling=$%.4f (task=%s).",
            session_cost, _HARD_KILL_MULTIPLIER, budget, task_id,
        )
        patch["security_flags"] = [_HARD_KILL_FLAG]
        await _force_dlq(
            state, task_id,
            reason="budget_hard_kill",
            detail=f"cost=${session_cost:.4f} > ceiling=${budget:.4f}",
        )
        return patch

    # === Trigger 4 — Soft HITL gate =======================================
    if session_cost > budget:
        from api.websocket_manager import vfs_manager

        result = await vfs_manager.request_human_approval(
            session_id=task_id,
            action_description="BUDGET_OVERFLOW",
            proposed_content=_format_budget_breach(session_cost, budget, snap),
            timeout_s=300.0,
            request_kind="BUDGET_OVERFLOW",
        )
        if result is not None and result.get("approved"):
            new_ceiling: float = budget * 2.0
            logger.warning(
                "Supervisor: BUDGET_OVERFLOW approved — ceiling $%.2f → $%.2f "
                "(task=%s).", budget, new_ceiling, task_id,
            )
            patch["session_max_budget_usd"] = new_ceiling
        else:
            logger.error(
                "Supervisor: BUDGET_OVERFLOW denied/timeout — hard kill (task=%s).",
                task_id,
            )
            patch["security_flags"] = [_HARD_KILL_FLAG]
            await _force_dlq(
                state, task_id,
                reason="budget_overflow_denied",
                detail=f"cost=${session_cost:.4f}, ceiling=${budget:.4f}",
            )
            return patch

    # === Trigger 5 — Token-spike trip (advisory, independent of budget) ====
    current_total: float = snap["local_tokens"] + snap["cloud_tokens"]
    last_total: float = _LAST_TURN_TOKENS.get(task_id, 0.0)
    _LAST_TURN_TOKENS[task_id] = current_total
    turn_delta: float = current_total - last_total
    max_per_turn: int = _max_tokens_per_turn()

    if turn_delta > max_per_turn:
        logger.warning(
            "Supervisor: TOKEN_SPIKE — %.0f tokens this turn > limit %d (task=%s).",
            turn_delta, max_per_turn, task_id,
        )
        from api.websocket_manager import vfs_manager

        result = await vfs_manager.request_human_approval(
            session_id=task_id,
            action_description="TOKEN_SPIKE",
            proposed_content=(
                f"Single-turn token usage {turn_delta:.0f} exceeded the "
                f"per-turn limit of {max_per_turn}. Approve to continue?"
            ),
            timeout_s=300.0,
            request_kind="TOKEN_SPIKE",
        )
        # Advisory only: a spike is not a budget breach. Denial is logged and
        # execution continues — the hard ceiling (Trigger 3) remains the
        # load-bearing financial firewall.
        if result is None or not result.get("approved"):
            logger.warning(
                "Supervisor: TOKEN_SPIKE not approved — advisory, continuing "
                "(task=%s).", task_id,
            )

    return patch


def route_after_supervisor(state: Dict[str, Any]) -> str:
    """Synchronous conditional edge: route to ``apply_patch`` or ``END``.

    LangGraph conditional-edge callbacks must be synchronous even when the
    preceding node is async. Reads the ``SESSION_BUDGET_HARD_KILL`` sentinel
    from ``security_flags`` (set by :func:`run_supervisor_node` on a terminal
    pass) to decide termination.
    """
    from core.telemetry import log_routing_decision

    flags = state.get("security_flags") or []
    if _HARD_KILL_FLAG in flags:
        target = "__end__"
    else:
        target = "apply_patch"
    log_routing_decision(
        session_id=state.get("task_id", ""),
        source="supervisor_node",
        target=target,
        reason="budget_hard_kill" if target == "__end__" else "within_budget",
        css=state.get("css"),
        tci=state.get("tci"),
    )
    logger.info("route_after_supervisor: → %s.", target)
    return target
