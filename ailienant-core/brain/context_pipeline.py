"""Five-Layer Context Pipeline — budget-aware, eviction-observable assembler.

Composes context from five semantically distinct layers with a dynamic budget
model that guarantees pinned layers (Foundation, Project, Memory) are never
silently dropped while the Conversation window FIFO-evicts under pressure and
Execution tool output is tail-truncated rather than discarded whole.

Layer budget fractions (nominal; actual is dynamic after L1-L3 anchor):
    L1  Foundation    20%   static; never evicted
    L2  Project       15%   session-persistent; never evicted
    L3  Memory        20%   rolling; oldest evicted on overflow
    L4  Conversation  30%   FIFO window; eviction fires STATE_COMPACTED callback
    L5  Execution     15%   volatile per-turn; tail-truncated on overflow

ContextChunk is defined here (not in agents/) so the dependency graph is
foundation-layer-up: brain/ ← agents/ ← gateway/, never the reverse.
"""
from __future__ import annotations

import logging
import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Awaitable, Callable, ClassVar, List, Optional

from tools.token_counter import PrecisionTokenCounter

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. ContextChunk — indivisible unit of context (moved from agents/analyst_context)
# ---------------------------------------------------------------------------


@dataclass
class ContextChunk:
    """One indivisible unit of context packed whole or not at all.

    ``brain`` is an opaque provenance/label tag (e.g. the producing layer or
    source); the pipeline treats it as an identifier for logging only.
    """

    body: str
    brain: str
    label: str
    tokens: int = field(default=0)

    def measure(self) -> "ContextChunk":
        self.tokens = PrecisionTokenCounter.count(self.body)
        return self


# ---------------------------------------------------------------------------
# 2. ContextAssemblyResult — observable return from assemble()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextAssemblyResult:
    """Immutable snapshot of one pipeline assembly pass.

    Callers use ``.content`` for LLM injection and the metric fields for
    telemetry, logging, or escalation decisions — without re-measuring.
    """

    content: str
    total_tokens: int
    l1_tokens: int
    l2_tokens: int
    l3_tokens: int
    l4_tokens: int
    l5_tokens: int
    l4_evicted: int     # conversation entries dropped by FIFO eviction this pass
    l5_truncated: bool  # True if any L5 chunk was tail-truncated this pass


# ---------------------------------------------------------------------------
# 3. ContextBudgetError — raised when the full window is exhausted by L1-L3
# ---------------------------------------------------------------------------


class ContextBudgetError(Exception):
    """Raised when L1-L3 anchored content alone fills the entire token budget.

    This is a hard error, not a graceful degradation, because silently removing
    Foundation/Project/Memory content would destroy the agent's grounding.
    """


# ---------------------------------------------------------------------------
# 4. ContextLayer ABC + 5 concrete implementations
# ---------------------------------------------------------------------------

_TRUNCATION_MARKER = "\n...[OUTPUT TRUNCATED BY PIPELINE TO FIT CONTEXT BUDGET]...\n"
# Measured once at module load so every assemble() call can pre-deduct cheaply.
_MARKER_TOKENS: int = PrecisionTokenCounter.count(_TRUNCATION_MARKER)

# Headroom reserved for layer section headers and structural whitespace injected
# during final assembly (5 headers × ~8 tokens + newlines ≈ 50 tokens).
_SAFETY_BUFFER: int = 50


class ContextLayer(ABC):
    """Abstract base for a single budget-managed context layer.

    Subclasses set three ClassVars (name, budget_fraction, can_evict) and
    inherit all runtime behaviour from this base — no override is needed.
    """

    name: ClassVar[str]
    budget_fraction: ClassVar[float]
    can_evict: ClassVar[bool] = True

    def __init__(self) -> None:
        self._chunks: List[ContextChunk] = []

    def add(self, chunk: ContextChunk) -> None:
        """Append a chunk; auto-measures if tokens == 0 (defensive)."""
        if chunk.tokens == 0:
            chunk = chunk.measure()
        self._chunks.append(chunk)

    def replace(self, chunks: List[ContextChunk]) -> None:
        """Replace the layer's content wholesale; auto-measures unmeasured chunks."""
        self._chunks = [c if c.tokens > 0 else c.measure() for c in chunks]

    def chunks(self) -> List[ContextChunk]:
        return list(self._chunks)

    def token_count(self) -> int:
        return sum(c.tokens for c in self._chunks)

    def evict_oldest(self, n: int) -> List[ContextChunk]:
        """Batch-evict the oldest n chunks (FIFO). No-op when can_evict is False."""
        if not self.can_evict or n <= 0:
            return []
        n = min(n, len(self._chunks))
        evicted, self._chunks = self._chunks[:n], self._chunks[n:]
        _log.info(
            "layer=%s evicted=%d remaining=%d", self.name, n, len(self._chunks)
        )
        return evicted


class FoundationLayer(ContextLayer):
    """L1 — system prompt, role identity, AILIENANT.md, tool schemas. Static."""
    name = "foundation"
    budget_fraction = 0.20
    can_evict = False


class ProjectLayer(ContextLayer):
    """L2 — README digest, GraphRAG project summary, rules. Session-persistent."""
    name = "project"
    budget_fraction = 0.15
    can_evict = False


class MemoryLayer(ContextLayer):
    """L3 — StateSummarizer output, checkpoint deltas, dreaming digest. Rolling."""
    name = "memory"
    budget_fraction = 0.20
    can_evict = True


class ConversationLayer(ContextLayer):
    """L4 — recent turns, WBS status, HITL decisions. FIFO eviction."""
    name = "conversation"
    budget_fraction = 0.30
    can_evict = True


class ExecutionLayer(ContextLayer):
    """L5 — tool results, diffs, benchmark reports. Volatile; tail-truncated."""
    name = "execution"
    budget_fraction = 0.15
    can_evict = True


# ---------------------------------------------------------------------------
# 5. ContextPipeline — per-session assembler
# ---------------------------------------------------------------------------

_LAYER_HEADER = "\n=== [{name}] ===\n"


class ContextPipeline:
    """Per-session context assembler. NOT a singleton — one instance per session.

    ``on_compacted`` is an optional async callback fired whenever Layer 4
    FIFO-evicts conversation turns. Signature: (compaction_message: str,
    turns_compressed: int) -> Awaitable[None]. Wire it to
    ``websocket_manager.broadcast_state_compacted`` via ``functools.partial``
    with the session_id bound.
    """

    def __init__(
        self,
        total_token_budget: int,
        *,
        on_compacted: Optional[Callable[[str, int], Awaitable[None]]] = None,
        session_id: Optional[str] = None,
        session_start_time: Optional[float] = None,
    ) -> None:
        self._total = total_token_budget
        self._on_compacted = on_compacted
        self._session_id = session_id
        self._session_start_time = session_start_time
        self._layers: tuple[ContextLayer, ...] = (
            FoundationLayer(),
            ProjectLayer(),
            MemoryLayer(),
            ConversationLayer(),
            ExecutionLayer(),
        )
        _log.debug("ContextPipeline init; budget=%d tokens", total_token_budget)

    # -- typed layer accessors -----------------------------------------------

    @property
    def foundation(self) -> FoundationLayer:
        return self._layers[0]  # type: ignore[return-value]

    @property
    def project(self) -> ProjectLayer:
        return self._layers[1]  # type: ignore[return-value]

    @property
    def memory(self) -> MemoryLayer:
        return self._layers[2]  # type: ignore[return-value]

    @property
    def conversation(self) -> ConversationLayer:
        return self._layers[3]  # type: ignore[return-value]

    @property
    def execution(self) -> ExecutionLayer:
        return self._layers[4]  # type: ignore[return-value]

    # -- main entry point ----------------------------------------------------

    async def assemble(self) -> ContextAssemblyResult:
        """Assemble all layers into one context string with dynamic budget management.

        Guarantee: L1-L3 are always fully included. If the remaining budget
        after L1-L3 is zero or negative, raises ContextBudgetError rather than
        silently dropping pinned content.
        """

        # Phase 1 — anchor L1-L3 (immutable; they take what they need)
        l1_tok = self.foundation.token_count()
        l2_tok = self.project.token_count()
        l3_tok = self.memory.token_count()
        anchored = l1_tok + l2_tok + l3_tok

        # Deduct safety buffer before computing the L4/L5 remainder so that
        # section headers injected during assembly never exceed the hard limit.
        effective_total = self._total - _SAFETY_BUFFER
        remaining = effective_total - anchored
        if remaining <= 0:
            raise ContextBudgetError(
                f"L1-L3 consumed {anchored} tokens which exhausts the effective "
                f"{effective_total}-token budget (raw={self._total}, "
                f"buffer={_SAFETY_BUFFER}). Reduce Foundation/Project/Memory content."
            )

        # Phase 2 — split remainder between L4 (2/3) and L5 (1/3)
        l4_budget = int(remaining * 2 / 3)
        l5_budget = remaining - l4_budget

        # Phase 3 — L4 batch FIFO eviction (O(n) scan, one list mutation)
        l4_evicted = 0
        l4_total = self.conversation.token_count()
        if l4_total > l4_budget:
            freed = 0
            to_drop = 0
            for chunk in self.conversation.chunks():  # oldest → newest
                if l4_total - freed <= l4_budget:
                    break
                freed += chunk.tokens
                to_drop += 1
            if to_drop:
                self.conversation.evict_oldest(to_drop)
                l4_evicted = to_drop

        if l4_evicted > 0 and self._on_compacted is not None:
            msg = (
                f"Compacted {l4_evicted} conversation turn(s) "
                "to fit the context window."
            )
            await self._on_compacted(msg, l4_evicted)

        # Phase 4 — L5 tail-truncation (token-exact: marker cost pre-deducted)
        l5_truncated = False
        l5_chunks = self.execution.chunks()
        l5_total = self.execution.token_count()
        if l5_total > l5_budget:
            n_chunks = len(l5_chunks)
            if n_chunks == 0 or l5_budget < _MARKER_TOKENS:
                # Budget too small to fit even one truncation stub — drop L5.
                self.execution.replace([])
                l5_truncated = True
                _log.info("L5 omitted entirely; l5_budget=%d < marker=%d", l5_budget, _MARKER_TOKENS)
            else:
                # Pre-deduct marker cost for every chunk so the assembled total
                # can never exceed l5_budget after re-measurement.
                effective_l5 = max(1, l5_budget - (_MARKER_TOKENS * n_chunks))
                ratio = effective_l5 / l5_total
                trimmed: List[ContextChunk] = []
                for c in l5_chunks:
                    max_chars = max(1, int(len(c.body) * ratio))
                    body = c.body[:max_chars] + _TRUNCATION_MARKER
                    trimmed.append(
                        ContextChunk(body=body, brain=c.brain, label=c.label).measure()
                    )
                self.execution.replace(trimmed)
                l5_truncated = True
                _log.info(
                    "L5 tail-truncated; n_chunks=%d effective_budget=%d tokens",
                    n_chunks, effective_l5,
                )

        # Phase 5 — assemble output string (L1 → L5, section-delimited)
        parts: List[str] = []
        for layer in self._layers:
            layer_chunks = layer.chunks()
            if not layer_chunks:
                continue
            header = _LAYER_HEADER.format(name=layer.name.upper())
            body = "\n\n".join(c.body for c in layer_chunks if c.body)
            parts.append(header + body)
        content = "\n".join(parts)

        l4_tok = self.conversation.token_count()
        l5_tok = self.execution.token_count()
        total_tok = l1_tok + l2_tok + l3_tok + l4_tok + l5_tok

        _log.debug(
            "assemble complete; total=%d l1=%d l2=%d l3=%d l4=%d l5=%d "
            "l4_evicted=%d l5_truncated=%s",
            total_tok, l1_tok, l2_tok, l3_tok, l4_tok, l5_tok,
            l4_evicted, l5_truncated,
        )
        try:
            from core.telemetry_log import log_context_utilization
            log_context_utilization(
                session_id=self._session_id or "", source="pipeline",
                total_tokens=total_tok, token_budget=self._total,
                turn_count=len(self.conversation.chunks()),
                duration_s=(time.time() - self._session_start_time) if self._session_start_time else 0.0,
                l1_tokens=l1_tok, l2_tokens=l2_tok, l3_tokens=l3_tok,
                l4_tokens=l4_tok, l5_tokens=l5_tok,
                l4_evicted=l4_evicted, l5_truncated=l5_truncated,
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort, never blocks assembly
            _log.debug("context-utilization telemetry emit failed", exc_info=True)
        return ContextAssemblyResult(
            content=content,
            total_tokens=total_tok,
            l1_tokens=l1_tok,
            l2_tokens=l2_tok,
            l3_tokens=l3_tok,
            l4_tokens=l4_tok,
            l5_tokens=l5_tok,
            l4_evicted=l4_evicted,
            l5_truncated=l5_truncated,
        )
