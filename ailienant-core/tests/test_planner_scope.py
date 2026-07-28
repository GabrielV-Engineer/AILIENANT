"""Planner scope-discipline + stack-guidance + output-budget guards.

A single-file request in a folder of unrelated documents used to sprawl into
hallucinated edits of those documents, because (a) the instruction carried no
scope discipline and (b) low-relevance retrievals were injected into the prompt.
These tests pin the guards that contain that behaviour, plus the proportional
scope wording, stack-choice guidance, and complexity-scaled draft budget added
to correct the live-test sweep's shallow-plan / wrong-stack findings.
"""
from __future__ import annotations

from agents.planner import (
    _SCOPE_DISCIPLINE_DIRECTIVE,
    _STACK_GUIDANCE_DIRECTIVE,
    _PLANNER_DRAFT_MIN_MAX_TOKENS,
    _PLANNER_DRAFT_MAX_MAX_TOKENS,
    _resolve_planner_draft_max_tokens,
)
from agents.researcher import _DEEP_CONTEXT_MIN_SIM


def test_scope_directive_constrains_to_requested_files() -> None:
    text = _SCOPE_DISCIPLINE_DIRECTIVE
    assert "SCOPE DISCIPLINE" in text
    # The two load-bearing rules: only requested files, context is read-only.
    assert "ONLY" in text
    assert "READ-ONLY" in text
    assert "NEVER a reason to edit" in text


def test_scope_directive_is_proportional_not_uniformly_minimal() -> None:
    """The old wording ('the smallest WBS is the correct one') under-planned
    broad build-outs — it must now call out that under-building a broad request
    is as much a violation as over-building a narrow one."""
    text = _SCOPE_DISCIPLINE_DIRECTIVE
    assert "broad" in text.lower()
    assert "under-building" in text.lower()


def test_stack_guidance_directive_biases_toward_artifact_class() -> None:
    """No stack bias existed anywhere in the prompt surface, so an unconstrained
    'build a game' request got a generic web stack (Django/React) from the
    model's own prior. The directive must name the corrective principle."""
    text = _STACK_GUIDANCE_DIRECTIVE
    assert "STACK CHOICE" in text
    assert "ARTIFACT CLASS" in text
    assert "game" in text.lower()


def test_deep_context_floor_is_a_sane_similarity_threshold() -> None:
    # A relevance floor must live strictly inside the (0, 1) similarity range,
    # or the gate is either a no-op (<=0) or suppresses everything (>=1).
    assert 0.0 < _DEEP_CONTEXT_MIN_SIM < 1.0


# ── Item C — complexity-scaled WBS draft output ceiling ───────────────────────


def test_planner_draft_tokens_never_below_flat_default_under_ample_budget() -> None:
    assert _resolve_planner_draft_max_tokens("fix the typo", 200_000) >= _PLANNER_DRAFT_MIN_MAX_TOKENS


def test_planner_draft_tokens_scale_with_request_length() -> None:
    short_ceiling = _resolve_planner_draft_max_tokens("fix the typo", 200_000)
    long_ceiling = _resolve_planner_draft_max_tokens("build an MVP " * 500, 200_000)
    assert long_ceiling > short_ceiling


def test_planner_draft_tokens_bounded_by_half_the_resolved_budget() -> None:
    ceiling = _resolve_planner_draft_max_tokens("build an MVP " * 5000, 200_000)
    assert ceiling <= 100_000


def test_planner_draft_tokens_real_context_window_wins_over_flat_floor() -> None:
    """A tiny context window must cap max_tokens at half its real budget even
    when that dips below the historical floor — see the matching coder-side
    test (test_coder_agent.py) for the same invariant on the generation call."""
    ceiling = _resolve_planner_draft_max_tokens("build an MVP " * 5000, 2048)
    assert ceiling <= 1024


def test_planner_draft_tokens_capped_at_max_ceiling() -> None:
    ceiling = _resolve_planner_draft_max_tokens("x" * 1_000_000, 10_000_000)
    assert ceiling <= _PLANNER_DRAFT_MAX_MAX_TOKENS
