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
    assert "artifact class" in text.lower()
    assert "game" in text.lower()


def test_stack_guidance_directive_is_a_procedure_not_a_hardcoded_catalog() -> None:
    """11.12 — a hardcoded ARTIFACT_CLASS_TO_STACK_OPTIONS catalog was proposed and
    rejected: it costs real tokens on every planner call, goes stale as the
    framework landscape moves, and overrides the model's own (fresher) knowledge
    with a hand-maintained one. The directive must stay a reasoning procedure —
    it may use a FEW illustrative examples (game/CLI/data pipeline), but must
    never enumerate specific competing frameworks by name (the exact staleness
    failure mode a catalog would introduce)."""
    text = _STACK_GUIDANCE_DIRECTIVE
    lowered = text.lower()
    # Illustrative examples are fine and expected (see the directive-biases test);
    # naming specific competing frameworks is the catalog anti-pattern being guarded.
    for framework in ("django", "react", "godot", "unity", "unreal", "flask", "fastapi"):
        assert framework not in lowered, f"directive hardcodes a specific framework: {framework}"


def test_stack_guidance_directive_requires_a_named_recorded_decision() -> None:
    """The choice must be recorded as decisions[0] in a fixed, greppable form —
    a vague 'a game engine' with no concrete name defeats propagation (Layer 3
    reads decisions[0] as the stack line under truncation pressure)."""
    text = _STACK_GUIDANCE_DIRECTIVE
    assert "decisions" in text.lower()
    assert "Stack:" in text
    assert "FIRST" in text or "first" in text.lower()


def test_stack_guidance_directive_requires_target_file_consistency() -> None:
    """Closes the contradiction the 11.11 version allowed: a plan naming Godot as
    the stack, then a WBS task targeting main.py."""
    text = _STACK_GUIDANCE_DIRECTIVE
    assert "target_file" in text
    assert "consistent" in text.lower()


def test_stack_guidance_directive_preserves_escape_hatches() -> None:
    """User-specified and workspace-matching stacks must still short-circuit the
    procedure — the directive only fires for a genuinely unconstrained request."""
    text = _STACK_GUIDANCE_DIRECTIVE
    assert "user named" in text.lower() or "user has not specified" in text.lower() or "user did not" in text.lower()
    assert "workspace" in text.lower()


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
