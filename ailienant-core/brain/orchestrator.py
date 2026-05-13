"""
brain/orchestrator.py — OrchestratorContext (Phase 4 stub).

Phase 2.5 scope: exposes get_partial_context_prefix() so PromptBuilder can
warn the LLM when workspace indexing is incomplete.
Full OrchestratorAgent wiring (WBS lifecycle, telemetry, prompt swapping) deferred to Phase 4.
"""
from __future__ import annotations

_WARNING_TEMPLATE = (
    "[SYSTEM WARNING: Workspace indexing is {pct:.0f}% complete. "
    "Context may be incomplete. Flag assumptions where architecture is unclear.]"
)


class OrchestratorContext:
    """Thin Phase 4 stub. Provides context-quality prefix for prompt assembly."""

    def get_partial_context_prefix(self) -> str:
        """Return warning string if indexing < 100%, empty string otherwise.

        Reads live state from lazy_indexer singleton each call (no caching) so
        the prefix accurately reflects current indexing progress.
        """
        try:
            from core.indexer import lazy_indexer
            if lazy_indexer.is_complete:
                return ""
            pct = lazy_indexer.progress_percentage
        except ImportError:
            return ""
        return "" if pct >= 100.0 else _WARNING_TEMPLATE.format(pct=pct)


# Global singleton — imported by brain/prompt_builder.py and agents
orchestrator_context = OrchestratorContext()
