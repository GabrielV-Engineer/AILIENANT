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

# The WBS draft's output-token ceiling used to be a planner-local function
# (`_resolve_planner_draft_max_tokens`) scaled against the whole-turn context
# budget. It has been replaced by `brain.agent_context.resolve_output_budget`,
# a joint input+output calculation shared with the coder — see
# tests/test_context_pipeline.py for its coverage, including the regression
# proving the old shape collapsed onto a single flat value regardless of the
# request's real length or the model's real window.
