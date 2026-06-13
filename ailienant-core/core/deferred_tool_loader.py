"""Eager-vs-deferred tool-injection policy over the existing ToolRAGStore.

This module is a thin POLICY layer above the storage/ranking MECHANISM in
``core/tool_rag.py``. It never mutates the store and never alters
``select_tools`` / ``prompt_size_metrics`` semantics (those are pinned by an
existing financial-reduction gate), so it cannot regress the reduction
guarantee — it only decides, per turn, whether the visible catalog is small
enough to inject whole, or large enough that relevance retrieval must kick in.

Decision in one line: if the role/session-visible tool payload fits under a
small fraction of the model's context budget, inject it whole (eager); above
that, fall back to ``select_tools`` top-k and always include ``tool_search`` so
the agent can pull the rest on demand.

The size comparison is intentionally done in CHARACTERS, matching
``ToolRAGStore.prompt_size_metrics`` (the unit the reduction target is measured
in). The context-window TOKEN budget is converted to characters with a coarse
heuristic — this is a "are we over ~10% of budget" gate, not a billing
measurement, so a precise tokenizer is deliberately kept off this hot path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Literal

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import (
    TOOL_RAG_TOP_K,
    ToolRAGStore,
    ToolSchema,
    tool_rag_store,
)

# =====================================================================
# Constants
# =====================================================================

TOOL_RAG_EAGER_THRESHOLD_FRAC: float = float(
    os.getenv("TOOL_RAG_EAGER_THRESHOLD_FRAC", "0.10")
)
"""Max fraction of the context budget the eager tool payload may consume before
relevance retrieval is triggered. Tunable via env for calibration."""

_CHARS_PER_TOKEN: float = 4.0
"""Coarse English chars/token heuristic used to convert the context-window token
budget into the char unit that prompt_size_metrics works in. Precision is
unnecessary for a threshold gate; PrecisionTokenCounter is kept off this path."""

_DEFAULT_CONTEXT_WINDOW: int = 8192
"""Fallback context window — matches brain.prompt_builder's default."""

_TOOL_SEARCH_NAME: str = "tool_search"
"""The universal discovery tool that must survive into every deferred set."""


# =====================================================================
# Decision record
# =====================================================================


@dataclass(frozen=True)
class DeferredToolDecision:
    """Outcome of one eager-vs-deferred resolution for a single turn."""

    mode: Literal["eager", "deferred"]
    schemas: List[ToolSchema]
    eager_count: int
    eager_chars: int
    threshold_chars: int
    reduction_ratio: float


# =====================================================================
# Loader
# =====================================================================


class DeferredToolLoader:
    """Decide, per turn, whether to inject the whole visible catalog or retrieve."""

    def __init__(self, store: ToolRAGStore = tool_rag_store) -> None:
        self._store = store

    @staticmethod
    def _visible_eager(
        store: ToolRAGStore,
        active_role: str,
        session_mode: SessionPermissionMode,
    ) -> List[ToolSchema]:
        """Every schema this (role, session_mode) pair would see without Tool RAG.

        This predicate MUST stay identical to the one tool_rag_select_node used
        before this loader existed (role membership + PLAN -> READ_ONLY only), so
        the eager baseline the reduction metric is measured against does not drift.
        """
        return [
            s
            for s in store.all_schemas()
            if active_role in s.allowed_roles
            and (
                session_mode is not SessionPermissionMode.PLAN
                or s.privilege_tier is ToolPrivilegeTier.READ_ONLY
            )
        ]

    @staticmethod
    def threshold_chars(context_window: int) -> int:
        """Char budget below which the eager payload is injected whole."""
        return int(
            context_window * _CHARS_PER_TOKEN * TOOL_RAG_EAGER_THRESHOLD_FRAC
        )

    async def resolve(
        self,
        intent: str,
        *,
        active_role: str,
        session_mode: SessionPermissionMode,
        context_window: int = _DEFAULT_CONTEXT_WINDOW,
        k: int = TOOL_RAG_TOP_K,
    ) -> DeferredToolDecision:
        """Return the schemas to inject this turn plus the audit metrics.

        Eager branch performs zero awaits (no embedding, no leak surface). The
        deferred branch makes exactly one ``select_tools`` await and guarantees
        ``tool_search`` is present without a fragile drop loop.
        """
        eager = self._visible_eager(self._store, active_role, session_mode)
        eager_chars = sum(len(s.json_schema) for s in eager)
        threshold = self.threshold_chars(context_window)

        if eager_chars <= threshold:
            schemas: List[ToolSchema] = eager
            mode: Literal["eager", "deferred"] = "eager"
        else:
            mode = "deferred"
            tool_search = next(
                (s for s in eager if s.name == _TOOL_SEARCH_NAME), None
            )
            if k <= 1:
                # select_tools(k=0) can still emit one tool via its READ_ONLY
                # guarantee, which would break the <= k bound. Short-circuit so
                # the deferred set is exactly the discovery tool (or empty if it
                # is not registered/visible).
                schemas = [tool_search] if tool_search is not None else []
            else:
                # Reserve one slot for tool_search: fetch k-1 and append. O(1),
                # <= k guaranteed, no conditional drop branch.
                schemas = await self._store.select_tools(
                    intent,
                    k=k - 1,
                    active_role=active_role,
                    session_mode=session_mode,
                )
                if tool_search is not None and not any(
                    s.name == _TOOL_SEARCH_NAME for s in schemas
                ):
                    schemas.append(tool_search)

        metrics = ToolRAGStore.prompt_size_metrics(eager, schemas)
        return DeferredToolDecision(
            mode=mode,
            schemas=schemas,
            eager_count=len(eager),
            eager_chars=eager_chars,
            threshold_chars=threshold,
            reduction_ratio=metrics["reduction_ratio"],
        )


# =====================================================================
# Module-level singleton (convenience handle for non-test callers)
# =====================================================================

deferred_tool_loader: DeferredToolLoader = DeferredToolLoader()
