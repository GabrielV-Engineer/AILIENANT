# brain/daemon.py
"""On-demand memory consolidation ("Dreaming").

The daemon never wakes on a timer. A consolidation pass runs only when the
operator explicitly asks for it (HUD button / VS Code command), routed through
``client_dreaming_run``. An idle trigger that woke GraphRAG+LLM mid-build would
overload the CPU, race a resuming typist, and burn tokens unattended — so the
user owns when resources are spent.

A pass reads a hard-bounded workspace overview, asks the LLM to consolidate
(optionally scoped to a ``focus_area`` theme to save tokens), and persists the
result as a semantic-memory note. The network call happens OUTSIDE the
per-project graph lock; only the final write is serialized under it. A save that
lands mid-run invalidates the snapshot (``stale_check``) so the pass aborts
without committing — the optimistic-concurrency discipline used across the write
path. Cancellation propagates cleanly: an aborted pass never leaves a partial
write.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agents.workspace_context import build_workspace_overview
from core.db import graph_write_lock
from shared.config import MODEL_MEDIUM

logger = logging.getLogger("OVERNIGHT_DAEMON")

# Bound the consolidation completion — a note, not an essay. Keeps the pass cheap.
_MAX_CONSOLIDATION_TOKENS: int = 1024

# Non-alphanumerics collapse to a single dash so the synthetic note path stays
# filesystem- and table-key-safe regardless of the free-text focus.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")

_CONSOLIDATION_SYSTEM = (
    "You are AILIENANT's memory-consolidation pass. You are read-only: you NEVER "
    "edit files. Given a workspace overview, distill durable architectural facts, "
    "recurring patterns, and latent technical debt into a compact note that will "
    "be embedded into long-term semantic memory. Be terse and concrete."
)


def _session_budget() -> float:
    """Session spend ceiling — same source of truth as the FinOps supervisor."""
    try:
        return float(os.getenv("AILIENANT_MAX_SESSION_BUDGET_USD", "5.00"))
    except ValueError:
        return 5.00


def _dream_path(focus_area: Optional[str]) -> str:
    """Stable per-focus note path so re-running a theme upserts in place."""
    slug = _SLUG_RE.sub("-", focus_area).strip("-").lower() if focus_area else "auto"
    return f".ailienant/dreams/{slug or 'auto'}.md"


@dataclass(frozen=True)
class ConsolidationResult:
    """Outcome of one consolidation pass — drives logging and tests."""

    status: str  # "written" | "refused_budget" | "aborted_stale" | "skipped_empty"
    chars: int
    focus_area: Optional[str]


class OvernightDaemon:
    """On-demand memory-consolidation service. Holds no timer and no loop.

    Injectable seams (``overview_fn``, ``budget_fn``, ``llm_invoke``,
    ``semantic``) default to the real implementations; tests substitute fakes.
    In-flight cancellation is owned by the caller (``main.py`` tracks one task
    per project), so this class stays lifecycle-thin.
    """

    def __init__(
        self,
        *,
        semantic: Optional[Any] = None,
        overview_fn: Callable[[str], str] = build_workspace_overview,
        budget_fn: Optional[Callable[[], Dict[str, float]]] = None,
        llm_invoke: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._semantic = semantic
        self._overview_fn = overview_fn
        self._budget_fn = budget_fn
        self._llm_invoke = llm_invoke
        self._running: bool = False

    # ── Lifecycle (lifespan symmetry; no background work) ─────────────────

    def start(self) -> None:
        """Mark the daemon ready. Idempotent; spawns nothing."""
        self._running = True
        logger.info("OvernightDaemon ready (on-demand consolidation).")

    async def stop(self) -> None:
        """Mark the daemon stopped. In-flight passes are cancelled by the caller."""
        self._running = False
        logger.info("OvernightDaemon stopped.")

    # ── Lazy seams (deferred so module import stays light) ────────────────

    def _budget(self) -> Dict[str, float]:
        if self._budget_fn is not None:
            return self._budget_fn()
        from core.token_ledger import token_ledger
        return token_ledger.snapshot()

    async def _invoke(self, messages: List[Dict[str, Any]], session_id: str) -> Any:
        if self._llm_invoke is not None:
            return await self._llm_invoke(
                messages, model=MODEL_MEDIUM,
                max_tokens=_MAX_CONSOLIDATION_TOKENS, session_id=session_id,
            )
        from tools.llm_gateway import LLMGateway
        return await LLMGateway.ainvoke(
            messages, model=MODEL_MEDIUM,
            max_tokens=_MAX_CONSOLIDATION_TOKENS, session_id=session_id,
        )

    async def _upsert(self, path: str, content: str, workspace_hash: str) -> bool:
        if self._semantic is None:
            from core.memory.semantic_memory import SemanticMemoryManager
            self._semantic = SemanticMemoryManager()
        result: bool = await self._semantic.semantic_upsert(
            path, content, workspace_hash=workspace_hash
        )
        return result

    # ── The pass ──────────────────────────────────────────────────────────

    async def run_consolidation(
        self,
        project_id: str,
        focus_area: Optional[str] = None,
        *,
        workspace_root: str,
        session_id: str,
        stale_check: Optional[Callable[[], bool]] = None,
    ) -> ConsolidationResult:
        """Run one consolidation pass. Safe to cancel at any await point.

        Refuses (no LLM call) when the session is already over budget. Aborts
        without writing when a concurrent save invalidates the snapshot. The
        per-project lock wraps ONLY the final write, never the network call.
        """
        # FinOps gate — the user owns spend, but an exhausted budget refuses.
        snap = self._budget()
        budget = _session_budget()
        if snap.get("estimated_invested_usd", 0.0) > budget:
            logger.warning(
                "Dreaming refused: session spend $%.4f over ceiling $%.2f (project=%s).",
                snap.get("estimated_invested_usd", 0.0), budget, project_id,
            )
            return ConsolidationResult("refused_budget", 0, focus_area)

        overview = self._overview_fn(workspace_root)
        if not overview:
            logger.info("Dreaming skipped: empty workspace overview (project=%s).", project_id)
            return ConsolidationResult("skipped_empty", 0, focus_area)

        focus_line = (
            f"Prioritize graph/memory restructuring toward: {focus_area}"
            if focus_area
            else "No specific focus — consolidate the whole workspace."
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _CONSOLIDATION_SYSTEM},
            {"role": "user", "content": f"{focus_line}\n\nWorkspace overview:\n{overview}"},
        ]

        # Network call outside the lock — never hold a DB lock across the wire.
        resp = await self._invoke(messages, session_id)
        try:
            content = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            content = ""
        if not content:
            logger.info("Dreaming produced no content (project=%s).", project_id)
            return ConsolidationResult("skipped_empty", 0, focus_area)

        # OCC commit guard — a save landed mid-run → abort without writing.
        if stale_check is not None and stale_check():
            logger.info("Dreaming aborted: snapshot invalidated mid-run (project=%s).", project_id)
            return ConsolidationResult("aborted_stale", 0, focus_area)

        async with graph_write_lock(project_id):
            await self._upsert(_dream_path(focus_area), content, project_id)
        logger.info(
            "Dreaming consolidated %d chars (project=%s, focus=%s).",
            len(content), project_id, focus_area or "auto",
        )
        return ConsolidationResult("written", len(content), focus_area)


# Module singleton — started in the FastAPI lifespan, fired by client_dreaming_run.
overnight_daemon = OvernightDaemon()
