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

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from brain.context_pipeline import (
    ContextAssemblyResult,
    ContextChunk,
    ContextLayer,
    ContextPipeline,
)

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
    when no profile is bound so the guard never assumes an unbounded window.
    """
    profile = state.get("active_llm_profile")
    ctx_window = int(getattr(profile, "context_window", 0) or 0)
    return ctx_window if ctx_window > 0 else DEFAULT_CONTEXT_BUDGET

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
