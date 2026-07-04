# ailienant-core/brain/nodes/dispatch_synthesize_node.py
"""The dispatch_synthesize graph node — fold a fan-out into one result.

Runs once after the final wave of subagents rejoins. It collects the per-worker
``SubagentResultEnvelope``s from the reducer-merged ``_dispatch_results`` channel,
applies a context-isolation digest bounded by the parent turn's own model window
(so a wide fan-out folding back into a small-window parent is trimmed harder than one
folding into a large-window parent), and writes exactly one ``DispatchBatchResult``.

The digest is whole-envelope greedy packing in declaration order under a char ceiling
derived from the parent's ``active_llm_profile`` — the same accumulate-until-ceiling,
whole-chunk discipline the GraphRAG extractor and the context budget-guard use. The
raw fan-in channel is intentionally not cleared: an ``operator.add`` channel cannot be
reset, and synthesis is terminal within a dispatch, so the accumulated raw results are
simply superseded on the next dispatch invocation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from brain.agent_context import resolve_context_budget
from brain.subagent_contracts import DispatchBatchResult, SubagentResultEnvelope

logger = logging.getLogger("DISPATCH_SYNTHESIZE")

# Chars-per-token conversion + the fraction of the parent window reserved for the
# batch digest. Mirrors the deferred-tool-loader's char-budget derivation so the
# ceiling scales with the parent's model, not a fixed constant.
_CHARS_PER_TOKEN: float = 4.0
_DIGEST_BUDGET_FRAC: float = 0.5


# Patterns that elect a single winning candidate.
_WINNER_PATTERNS = frozenset({"tournament", "generate_and_filter"})


def _select_winner(results: List[Dict[str, Any]]) -> Optional[str]:
    """Deterministic winner among succeeded envelopes for tournament/filter patterns.

    Prefers the highest ``score`` (a float/int field in ``structured_result`` by
    convention); falls back to the first ``ok`` result. A caller that owns a real
    verify harness can instead run the MCTS tournament via
    ``brain.subagent_tournament.run_tournament_from_dispatch`` and pass its
    ``winner_task_id`` in — this node's default keeps the pattern exercisable without
    a live surface/verify plumbing.
    """
    best_id: Optional[str] = None
    best_score = float("-inf")
    first_ok: Optional[str] = None
    for env in results:
        if str(env.get("status", "")) != "ok":
            continue
        task_id = str(env.get("task_id", ""))
        if first_ok is None:
            first_ok = task_id
        structured = env.get("structured_result")
        if isinstance(structured, dict):
            raw_score = structured.get("score")
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
                if float(raw_score) > best_score:
                    best_score = float(raw_score)
                    best_id = task_id
    return best_id if best_id is not None else first_ok


async def dispatch_synthesize(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Digest one dispatch's results into a DispatchBatchResult and reconcile budget.

    Terminal per top-level dispatch: it digests/commits only the envelopes since the
    consume-watermark (so a second dispatch in the same run never re-digests or
    double-counts the first), advances the watermark, commits the round's budget
    reservation (refunding the unused delta), and clears ``dispatch_plan`` so a stale
    plan cannot re-fire an origin edge after the subgraph returns.
    """
    all_results: List[Dict[str, Any]] = list(state.get("_dispatch_results") or [])
    consumed = int(state.get("_dispatch_consumed", 0) or 0)
    raw_results = all_results[consumed:]  # this dispatch's envelopes only (R-A)
    raw_plan: Dict[str, Any] = state.get("dispatch_plan") or {}
    pattern = str(raw_plan.get("pattern", ""))

    char_ceiling = int(resolve_context_budget(state) * _CHARS_PER_TOKEN * _DIGEST_BUDGET_FRAC)

    # Cost is always fully accounted, even for envelopes trimmed from the digest.
    total_cost = sum(float(e.get("cost_usd", 0.0) or 0.0) for e in raw_results)

    # Whole-envelope greedy pack in declaration order under the ceiling: keep whole
    # results until the next would overflow, then stop (never a partial envelope).
    packed: List[SubagentResultEnvelope] = []
    used = 0
    for env in raw_results:
        digest_len = len(str(env.get("raw_digest", "")))
        if packed and used + digest_len > char_ceiling:
            break
        try:
            packed.append(SubagentResultEnvelope.model_validate(env))
        except Exception as exc:  # noqa: BLE001 — a malformed envelope must not sink the batch
            logger.warning("dropping malformed dispatch result envelope: %s", exc)
            continue
        used += digest_len

    winner = _select_winner(raw_results) if pattern in _WINNER_PATTERNS else None

    batch = DispatchBatchResult(
        batch_id=uuid4().hex,
        pattern=pattern,
        results=packed,
        total_cost_usd=total_cost,
        winner_task_id=winner,
    )

    delta: Dict[str, Any] = {
        "dispatch_batch_result": batch.model_dump(),
        # Advance the watermark past every envelope observed so far, isolating a
        # subsequent dispatch in the same run. Clear the plan so its origin edge does
        # not re-fire on the way back out (R-C).
        "_dispatch_consumed": len(all_results),
        "dispatch_plan": None,
    }

    # Reconcile the round's budget reservation against the actual spend (R-D). A
    # refund folds negatively into the operator.add cost channel.
    reserved = float(state.get("_dispatch_reserved_usd", 0.0) or 0.0)
    if reserved > 0.0:
        from brain.dispatch_ledger import DispatchReservation, commit_dispatch_actual

        refund = commit_dispatch_actual(
            DispatchReservation(
                task_id=str(state.get("task_id", "")),
                batch_id=batch.batch_id,
                reserved_usd=reserved,
            ),
            total_cost,
        )
        if refund:
            delta["current_cost_usd"] = -refund
        delta["_dispatch_reserved_usd"] = 0.0

    return delta
