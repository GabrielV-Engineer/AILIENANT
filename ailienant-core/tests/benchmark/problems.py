"""Benchmark problem fixtures.

The scaffold ships a single trivial problem to exercise the four-arm smoke. The
frozen pinned-commit corpus and golden patches that make the real custom
multi-file benchmark defensible are introduced in a later phase.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tests.benchmark.oracle import CorpusProblem


@dataclass(frozen=True)
class BenchmarkProblem:
    """One coding problem the harness drives through the pipeline."""

    problem_id: str
    prompt: str
    workspace_root: str
    # When present, the runner submits the generated patch to the oracle for verdict scoring.
    corpus_problem: Optional["CorpusProblem"] = None
    corpus_root: Optional[Path] = None
    # Must equal the id used to index the corpus (benchmark-invalidating if mismatched).
    project_id: Optional[str] = None

    @classmethod
    def from_corpus(cls, cp: "CorpusProblem", corpus_root: Path) -> "BenchmarkProblem":
        """Build a BenchmarkProblem from a frozen CorpusProblem.

        The project_id is derived from the corpus directory name so the indexer
        and the payload always share the same key.
        """
        effective_id = f"benchmark_corpus_{corpus_root.name}"
        return cls(
            problem_id=cp.task_id,
            prompt=cp.prompt,
            workspace_root=str(corpus_root),
            corpus_problem=cp,
            corpus_root=corpus_root,
            project_id=effective_id,
        )


DUMMY_PROBLEM = BenchmarkProblem(
    problem_id="dummy-0001",
    prompt="Add a function that returns the sum of two integers.",
    workspace_root=".",
)
