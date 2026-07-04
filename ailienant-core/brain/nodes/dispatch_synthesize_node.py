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


async def dispatch_synthesize(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Digest the accumulated dispatch results into one DispatchBatchResult."""
    raw_results: List[Dict[str, Any]] = list(state.get("_dispatch_results") or [])
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

    batch = DispatchBatchResult(
        batch_id=uuid4().hex,
        pattern=pattern,
        results=packed,
        total_cost_usd=total_cost,
        winner_task_id=None,  # tournament selection populates this in a later sub-phase
    )
    return {"dispatch_batch_result": batch.model_dump()}
