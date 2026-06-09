"""Planner scope-discipline guards.

A single-file request in a folder of unrelated documents used to sprawl into
hallucinated edits of those documents, because (a) the instruction carried no
scope discipline and (b) low-relevance retrievals were injected into the prompt.
These tests pin the two guards that contain that behaviour.
"""
from __future__ import annotations

from agents.planner import _DEEP_CONTEXT_MIN_SIM, _SCOPE_DISCIPLINE_DIRECTIVE


def test_scope_directive_constrains_to_requested_files() -> None:
    text = _SCOPE_DISCIPLINE_DIRECTIVE
    assert "SCOPE DISCIPLINE" in text
    # The two load-bearing rules: only requested files, context is read-only.
    assert "ONLY" in text
    assert "READ-ONLY" in text
    assert "NEVER a reason to edit" in text


def test_deep_context_floor_is_a_sane_similarity_threshold() -> None:
    # A relevance floor must live strictly inside the (0, 1) similarity range,
    # or the gate is either a no-op (<=0) or suppresses everything (>=1).
    assert 0.0 < _DEEP_CONTEXT_MIN_SIM < 1.0
