"""Benchmark problem fixtures.

The scaffold ships a single trivial problem to exercise the four-arm smoke. The
frozen pinned-commit corpus and golden patches that make the real custom
multi-file benchmark defensible are introduced in a later phase.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkProblem:
    """One coding problem the harness drives through the pipeline."""

    problem_id: str
    prompt: str
    workspace_root: str


DUMMY_PROBLEM = BenchmarkProblem(
    problem_id="dummy-0001",
    prompt="Add a function that returns the sum of two integers.",
    workspace_root=".",
)
