"""Agent context budget-guard — a thin builder over :class:`ContextPipeline`.

Agents (planner, coder) historically concatenated their system/user prompts as
unbounded strings: identity + rules + project instructions + memory + the
volatile IDE context (open file, dirty buffers, RAG snippets). Under a large
task that silently overflows the model's context window, with no guarantee about
*what* gets dropped.

This builder routes those existing blocks through the five-layer pipeline so the
budget is enforced with a fixed priority: the durable instruction context
(Foundation/Project/Memory — identity, rules, memory) is never silently
truncated, while the volatile Execution layer (file/RAG content) is
tail-truncated first. It returns the post-assembly layer blocks as plain strings
so each agent keeps its own boundary-tag sandboxing, role split, and
response-cache key — only the budget discipline is added.

Lives in ``brain/`` (not ``agents/``) so the dependency direction stays
foundation-up: ``brain/`` ← ``agents/``, never the reverse.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from brain.context_pipeline import (
    ContextAssemblyResult,
    ContextChunk,
    ContextLayer,
    ContextPipeline,
)

logger = logging.getLogger("AGENT_CONTEXT")

# Fallback per-turn token budget used only when the ResourceBroker has not yet
# resolved an active LLM profile (a cache-hit turn or a benchmark stub). Kept
# conservative so the budget-guard still trims rather than assuming an unbounded
# window.
DEFAULT_CONTEXT_BUDGET: int = 8192

# Injected into the user turn when the L1-L3 anchored context alone exhausts the
# window and an agent degrades to an identity-only system prompt. Makes the model
# aware of its partial amnesia so it cannot hallucinate house style, Git, or
# security policy it can no longer see.
AMNESIA_ALERT: str = (
    "[SYSTEM ALERT: Context limits exceeded — global rules and project memory are "
    "disabled for this turn. Proceed with extreme caution; do NOT hallucinate style "
    "rules, Git, or security policy. Ask the user for clarification if project "
    "context is required.]"
)


def resolve_context_budget(state: Mapping[str, Any]) -> int:
    """Resolve the per-turn token budget from the active LLM profile.

    Mirrors the researcher's defensive read; falls back to a conservative constant
    when no profile is bound so the guard never assumes an unbounded window. This
    is the LAST-RESORT fallback layer — prefer :func:`resolve_real_window` when a
    concrete tier is known, since ``active_llm_profile`` is almost never bound in
    practice (see brain/agent_context.py's own callers) and this constant is a
    declared default, not a measurement of what the model actually serves.
    """
    profile = state.get("active_llm_profile")
    ctx_window = int(getattr(profile, "context_window", 0) or 0)
    return ctx_window if ctx_window > 0 else DEFAULT_CONTEXT_BUDGET


async def resolve_real_window(state: Mapping[str, Any], tier: str = "big") -> int:
    """Resolve the ACTUAL served context window for ``tier``, probed from the
    runtime rather than trusted from a declared profile.

    Falls back to :func:`resolve_context_budget` when the tier cannot be
    resolved to a concrete target, or when the runtime probe returns unknown
    (a non-Ollama provider, or a transient probe failure) — never raises, and
    never returns a number the caller could mistake for "definitely wrong":
    an unresolved probe simply defers to the existing conservative behaviour.
    """
    try:
        from core.config.model_resolver import get_chat_target, probe_runtime_capabilities

        target = get_chat_target(tier)
        if target is not None:
            caps = await probe_runtime_capabilities(target)
            if caps.context_length is not None and caps.context_length > 0:
                return caps.context_length
    except Exception:  # noqa: BLE001 — a probe fault must degrade, never block the caller
        logger.debug("resolve_real_window: probe failed for tier=%s", tier, exc_info=True)
    return resolve_context_budget(state)


# Headroom reserved on every output-budget call for chat-template overhead,
# special tokens, and the safety margin `PrecisionTokenCounter.estimate_with_buffer`
# does not itself cover on the OUTPUT side (it inflates the INPUT measurement).
OUTPUT_BUDGET_MARGIN_TOKENS: int = 256

# Below this many remaining tokens, refusing the call outright is more honest
# than attempting a completion that cannot hold a usable response — the exact
# case that used to surface as a silently truncated, unparseable draft several
# minutes later instead of an immediate, actionable refusal.
OUTPUT_BUDGET_MIN_USABLE_TOKENS: int = 256


@dataclass(frozen=True)
class OutputBudgetDecision:
    """Result of reconciling a requested output ceiling against the real window.

    ``max_tokens`` is meaningful only when ``ok`` is True. When ``ok`` is False
    the caller MUST refuse the LLM call and surface ``reason`` to the user —
    issuing the call anyway relocates the failure from here (an immediate,
    named refusal) to a truncated response discovered minutes later.
    """
    ok: bool
    max_tokens: int
    real_window: int
    prompt_tokens: int
    reason: Optional[str] = None


def resolve_output_budget(
    *,
    prompt_tokens: int,
    real_window: int,
    declared_ceiling: int,
    reserved_tokens: int = 0,
    margin_tokens: int = OUTPUT_BUDGET_MARGIN_TOKENS,
    min_usable_tokens: int = OUTPUT_BUDGET_MIN_USABLE_TOKENS,
) -> OutputBudgetDecision:
    """The one joint-budget calculation every structured-output caller needs.

    ``max_tokens = min(declared_ceiling, real_window - prompt_tokens - reserved - margin)``.

    This replaces the historical ``min(ceiling, budget // 2)`` shape
    (``agents/planner.py::_resolve_planner_draft_max_tokens``,
    ``agents/coder.py::_resolve_coder_max_tokens``): that arithmetic measured
    neither the real prompt nor the real window, so at the ONLY budget the
    system ever actually resolved (``DEFAULT_CONTEXT_BUDGET // 2`` = 4096) the
    floor and the ceiling collapsed onto the same value — every caller received
    exactly 4096 regardless of what it asked for. ``prompt_tokens`` and
    ``real_window`` must be REAL measurements (see
    ``PrecisionTokenCounter.estimate_with_buffer`` and ``resolve_real_window``),
    not declared constants, or this function reproduces the exact bug it exists
    to fix.
    """
    available = real_window - prompt_tokens - reserved_tokens - margin_tokens
    if available < min_usable_tokens:
        return OutputBudgetDecision(
            ok=False,
            max_tokens=0,
            real_window=real_window,
            prompt_tokens=prompt_tokens,
            reason=(
                f"the prompt ({prompt_tokens} tokens) leaves only {available} of the "
                f"model's real {real_window}-token window after reserving "
                f"{reserved_tokens + margin_tokens} tokens for overhead — below the "
                f"{min_usable_tokens}-token floor needed for a usable response. "
                "Shorten the request, free up context, or use a larger-window model."
            ),
        )
    return OutputBudgetDecision(
        ok=True,
        max_tokens=int(min(declared_ceiling, available)),
        real_window=real_window,
        prompt_tokens=prompt_tokens,
    )

# Joins chunk bodies with the same blank-line separator the agents already use
# when they append rule/instruction blocks (``system_prompt += f"\n\n{...}"``),
# so the assembled block matches the historical prompt shape. The pipeline's
# own ``=== [LAYER] ===`` section headers are an internal artifact of
# ``assemble().content`` and are deliberately NOT emitted into the agent blocks.
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class AgentContextResult:
    """Post-assembly layer blocks ready for an agent to splice into its messages.

    ``foundation_block`` (L1+L2+L3) is the durable instruction context and is
    guaranteed whole — a caller puts it in the system message. ``execution_block``
    (L5) is the budget-trimmed volatile content for the user message.
    ``assembly`` carries the per-layer token metrics for telemetry without a
    re-measure.
    """

    foundation_block: str
    conversation_block: str
    execution_block: str
    assembly: ContextAssemblyResult


def _add_sources(layer: ContextLayer, sources: Sequence[str]) -> None:
    """Add each non-empty source string to a layer as a measured chunk."""
    for idx, body in enumerate(sources):
        if not body:
            continue  # drop falsy/empty sources — no blank chunks, no \n\n artifacts
        layer.add(ContextChunk(body=body, brain=layer.name, label=f"{layer.name}-{idx}"))


def _join(*layers: ContextLayer) -> str:
    """Join the post-assembly chunk bodies of one or more layers in order."""
    return _BLOCK_SEPARATOR.join(
        c.body for layer in layers for c in layer.chunks() if c.body
    )


async def build_agent_context(
    *,
    total_token_budget: int,
    foundation: Sequence[str],
    project: Sequence[str] = (),
    memory: Sequence[str] = (),
    conversation: Sequence[str] = (),
    execution: Sequence[str] = (),
    on_compacted: Optional[Callable[[str, int], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    session_start_time: Optional[float] = None,
) -> AgentContextResult:
    """Assemble an agent's context under a hard token budget.

    Maps the caller's pre-built blocks onto the five pipeline layers, runs one
    budget-enforced assembly pass (L4 FIFO eviction + L5 tail-truncation happen
    in place), then reads each layer's surviving content back into plain blocks.

    Raises :class:`brain.context_pipeline.ContextBudgetError` when Foundation +
    Project + Memory alone exhaust the window — the caller is responsible for the
    amnesia-aware degrade path (Foundation/Project/Memory must never be silently
    dropped).
    """
    pipeline = ContextPipeline(
        total_token_budget,
        on_compacted=on_compacted,
        session_id=session_id,
        session_start_time=session_start_time,
    )
    _add_sources(pipeline.foundation, foundation)
    _add_sources(pipeline.project, project)
    _add_sources(pipeline.memory, memory)
    _add_sources(pipeline.conversation, conversation)
    _add_sources(pipeline.execution, execution)

    assembly = await pipeline.assemble()

    return AgentContextResult(
        foundation_block=_join(pipeline.foundation, pipeline.project, pipeline.memory),
        conversation_block=_join(pipeline.conversation),
        execution_block=_join(pipeline.execution),
        assembly=assembly,
    )
