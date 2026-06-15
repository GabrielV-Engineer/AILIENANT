"""Hermetic gate for the multi-file benchmark oracle and Resolve@k scorer.

No live model, no Docker, no real indexer. A scripted completion returns
hand-crafted patch responses; SubprocessPythonExecutor runs the test suite on
the host. The gate proves:

* The oracle genuinely executes the patched code (a wrong patch must fail).
* Resolve@k is reproducible across two identical runs.
* The extractor correctly maps filename-tagged fence blocks to file paths and
  rejects ambiguous untagged multi-target responses.
* The AST safety check fires before any subprocess is spawned.
* The indexer is awaited before measuring and empty dependents abort the run.
* The corpus loads a pinned SHA and three well-formed problems.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List

import pytest

from core.benchmark.codegen import Language
from core.benchmark.executors import SubprocessPythonExecutor
from core.benchmark.hygiene import BenchmarkAbort
from core.benchmark.oracle import (
    BenchmarkOracle,
    CorpusProblem,
    ResolveKReport,
    Verdict,
    _check_patch_safety,
    evaluate_resolve_k,
    extract_patch,
    load_corpus,
)

_Completion = Callable[[List[Dict[str, str]]], Awaitable[str]]


# --------------------------------------------------------------------------- #
# Shared fixtures and helpers                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the indexer and dependency DB for hermetic evaluate_resolve_k tests.

    The indexer marks itself complete immediately; get_dependents returns a
    non-empty list so the pre-run assert passes.
    """

    async def _instant_start(
        self: Any, workspace_root: str, project_id: str, session_id: str
    ) -> None:
        self._is_complete = True
        if self._complete_event is not None:
            self._complete_event.set()

    async def _fake_dependents(target: str, project_id: str = "") -> List[str]:
        return ["src/processor.py"]

    monkeypatch.setattr("core.indexer.LazyIndexer.start", _instant_start)
    monkeypatch.setattr("core.db.get_dependents", _fake_dependents)


def _scripted(responses: List[str]) -> _Completion:
    """Completion that returns canned responses in call order (serial runs)."""
    iterator = iter(responses)

    async def _complete(_messages: List[Dict[str, str]]) -> str:
        return next(iterator)

    return _complete


def _patch_response(problem: CorpusProblem) -> str:
    """Format the golden patch as filename-tagged fenced blocks."""
    return "\n".join(
        f"```python {rel}\n{content}```"
        for rel, content in problem.golden_patch.items()
    )


def _wrong_patch_response(problem: CorpusProblem) -> str:
    """Syntactically valid but semantically empty patch — imports will fail."""
    return "\n".join(
        f"```python {rel}\n\"\"\"wrong implementation\"\"\"\n__all__ = []\n```"
        for rel in problem.golden_patch
    )


def _make_oracle(corpus_root: Any) -> BenchmarkOracle:
    return BenchmarkOracle(corpus_root, SubprocessPythonExecutor())


# --------------------------------------------------------------------------- #
# Resolve@k correctness                                                         #
# --------------------------------------------------------------------------- #


def test_resolve_k_perfect_on_golden_patch(mock_infra: None) -> None:
    corpus_root, problems = load_corpus("v1")
    responses = [_patch_response(p) for p in problems]
    report = asyncio.run(
        evaluate_resolve_k(
            problems,
            corpus_root=corpus_root,
            completion=_scripted(responses),
            oracle=_make_oracle(corpus_root),
        )
    )
    assert report.total == 3
    assert report.resolved == 3
    assert report.resolve_at_k == 1.0
    assert all(v.passed for v in report.verdicts)


def test_resolve_k_zero_on_wrong_patch(mock_infra: None) -> None:
    corpus_root, problems = load_corpus("v1")
    responses = [_wrong_patch_response(p) for p in problems]
    report = asyncio.run(
        evaluate_resolve_k(
            problems,
            corpus_root=corpus_root,
            completion=_scripted(responses),
            oracle=_make_oracle(corpus_root),
        )
    )
    assert report.resolved == 0
    assert report.resolve_at_k == 0.0
    # All verdicts must reflect actual execution failure, not a skipped run.
    assert all(not v.passed for v in report.verdicts)


def test_resolve_k_reproducible(mock_infra: None) -> None:
    corpus_root, problems = load_corpus("v1")

    def _run() -> ResolveKReport:
        responses = [_patch_response(p) for p in problems]
        return asyncio.run(
            evaluate_resolve_k(
                problems,
                corpus_root=corpus_root,
                completion=_scripted(responses),
                oracle=_make_oracle(corpus_root),
            )
        )

    first, second = _run(), _run()
    assert (first.total, first.resolved, first.resolve_at_k) == (
        second.total,
        second.resolved,
        second.resolve_at_k,
    )


# --------------------------------------------------------------------------- #
# Extractor                                                                     #
# --------------------------------------------------------------------------- #


def test_extract_patch_maps_filename_in_fence_tag() -> None:
    text = "```python src/math_utils.py\ndef clamp(v, lo, hi): return lo\n```"
    patch = extract_patch(text, ("src/math_utils.py",))
    assert "src/math_utils.py" in patch
    assert "def clamp" in patch["src/math_utils.py"]


def test_extract_patch_single_block_fallback() -> None:
    # Untagged block + single target → fallback maps to that target.
    text = "```python\ndef clamp(v, lo, hi): return lo\n```"
    patch = extract_patch(text, ("src/math_utils.py",))
    assert patch == {"src/math_utils.py": "def clamp(v, lo, hi): return lo"}


def test_extract_patch_multi_target_no_tag_returns_empty() -> None:
    # Two targets, no filename in fence tag — must not guess.
    text = "```python\ndef clamp(v, lo, hi): return lo\n```"
    patch = extract_patch(text, ("src/math_utils.py", "src/processor.py"))
    assert patch == {}


# --------------------------------------------------------------------------- #
# AST safety                                                                    #
# --------------------------------------------------------------------------- #


def test_safety_blocks_dangerous_import() -> None:
    corpus_root, problems = load_corpus("v1")
    problem = problems[0]
    evil_patch = {"src/math_utils.py": "import os\ndef clamp(v, lo, hi): return lo\n"}

    class _NeverCalledExecutor:
        async def run(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
            raise AssertionError("executor must not be called after a safety block")

    oracle = BenchmarkOracle(corpus_root, _NeverCalledExecutor())  # type: ignore[arg-type]
    verdict = asyncio.run(oracle.run_oracle(problem, evil_patch))
    assert verdict.passed is False
    assert any("safety_blocked" in f for f in verdict.failures)


def test_check_patch_safety_blocks_getattr() -> None:
    reason = _check_patch_safety("x = getattr(obj, 'name')\n", "test.py")
    assert reason is not None
    assert "getattr" in reason


def test_check_patch_safety_allows_pure_math() -> None:
    reason = _check_patch_safety(
        "def clamp(v, lo, hi):\n    return lo if v < lo else hi if v > hi else v\n",
        "test.py",
    )
    assert reason is None


# --------------------------------------------------------------------------- #
# Indexer integration                                                           #
# --------------------------------------------------------------------------- #


def test_indexer_awaited_and_time_recorded(mock_infra: None) -> None:
    corpus_root, problems = load_corpus("v1")
    responses = [_patch_response(p) for p in problems]
    report = asyncio.run(
        evaluate_resolve_k(
            problems,
            corpus_root=corpus_root,
            completion=_scripted(responses),
            oracle=_make_oracle(corpus_root),
        )
    )
    # indexing_time_s is always present as a separate field (DoD).
    assert report.indexing_time_s >= 0.0
    assert report.total == 3


def test_empty_dependents_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus_root, problems = load_corpus("v1")

    async def _instant_start(
        self: Any, workspace_root: str, project_id: str, session_id: str
    ) -> None:
        self._is_complete = True
        if self._complete_event is not None:
            self._complete_event.set()

    async def _empty_dependents(target: str, project_id: str = "") -> List[str]:
        return []

    monkeypatch.setattr("core.indexer.LazyIndexer.start", _instant_start)
    monkeypatch.setattr("core.db.get_dependents", _empty_dependents)

    with pytest.raises(BenchmarkAbort, match="get_dependents"):
        asyncio.run(
            evaluate_resolve_k(
                problems,
                corpus_root=corpus_root,
                completion=_scripted([]),
                oracle=_make_oracle(corpus_root),
            )
        )


# --------------------------------------------------------------------------- #
# Corpus fixture                                                                #
# --------------------------------------------------------------------------- #


def test_corpus_loads_pinned_sha_and_three_problems() -> None:
    corpus_root, problems = load_corpus("v1")

    meta = json.loads((corpus_root / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("pinned_sha"), "meta.json must carry a non-empty pinned_sha"

    assert len(problems) == 3
    for p in problems:
        assert p.language is Language.PYTHON
        assert p.golden_patch
        assert p.test_body
        assert p.dependency_seed
        assert p.target_files

    # The dependency seed for each problem names a real source file.
    for p in problems:
        seed_rel = p.dependency_seed.removeprefix("src/")
        seed_path = corpus_root / "src" / seed_rel
        assert seed_path.exists(), f"{p.dependency_seed!r} not found in corpus"
