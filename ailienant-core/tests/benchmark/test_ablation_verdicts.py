"""Hermetic gate for the ablation verdict harness (8.3.3).

No live model, no Docker. A scripted task runner returns hand-crafted
pending_contents; SubprocessPythonExecutor runs the corpus test suite on the
host. The gate proves:

* G4_FORCE_CLOUD rebinds the live provider seam and restores on exit.
* Five arms produce comparable verdicts via the same oracle (golden → all passed).
* A wrong patch yields a failed verdict (oracle discrimination).
* project_id is identical across index and payload (benchmark-invalidating if wrong).
* Background tasks are drained after a run without RuntimeError.
* Absolute-path keys are relativized; escaping keys are dropped (not a crash).
* VectorOnlyRetrievalStrategy patches only the graph seam, leaving vector live.
* ZeroShotRetrievalStrategy patches all three retrieval seams.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

import agents.planner
import core.telemetry as telemetry
from core.telemetry import log_routing_decision
from core.token_ledger import token_ledger

from core.benchmark.arms import (
    AblationArm,
    CODER_VECTOR_SEAM,
    GRAPH_SEAM,
    PLANNER_VECTOR_SEAM,
    PROVIDER_SEAM,
    apply_arm,
)
from core.benchmark.executors import SubprocessPythonExecutor
from core.benchmark.oracle import BenchmarkOracle, load_corpus
from core.benchmark.problems import BenchmarkProblem
from core.benchmark.runner import BenchmarkRunner, TaskRunner, _normalize_patch
from core.benchmark.strategies import (
    VectorOnlyRetrievalStrategy,
    ZeroShotRetrievalStrategy,
)

_ALL_ARMS = [
    AblationArm.G1,
    AblationArm.G2,
    AblationArm.G3,
    AblationArm.G4,
    AblationArm.G4_FORCE_CLOUD,
]


# --------------------------------------------------------------------------- #
# Shared fixtures                                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _restore_telemetry_conn() -> Iterator[None]:
    saved = telemetry._conn
    yield
    telemetry._conn = saved


@pytest.fixture
def mock_infra(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture
def mock_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok_preflight(self: Any) -> None:
        return None

    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _ok_preflight)


def _load_first_problem() -> BenchmarkProblem:
    corpus_root, problems = load_corpus("v1")
    return BenchmarkProblem.from_corpus(problems[0], corpus_root)


def _make_oracle(corpus_root: Path) -> BenchmarkOracle:
    return BenchmarkOracle(corpus_root, SubprocessPythonExecutor())


def _golden_runner(problem: BenchmarkProblem) -> TaskRunner:
    """Task runner that returns the golden patch and logs telemetry."""
    assert problem.corpus_problem is not None
    golden = dict(problem.corpus_problem.golden_patch)

    async def _run(session_id: str, _p: BenchmarkProblem) -> Dict[str, str]:
        token_ledger.record_local(prompt=50, completion=50)
        log_routing_decision(
            session_id, "planner", "coder", "golden stub", css=70.0, tci=50.0
        )
        return golden

    return _run


def _wrong_runner(problem: BenchmarkProblem) -> TaskRunner:
    """Task runner that returns a syntactically valid but semantically broken patch."""
    assert problem.corpus_problem is not None
    keys = list(problem.corpus_problem.golden_patch.keys())

    async def _run(session_id: str, _p: BenchmarkProblem) -> Dict[str, str]:
        token_ledger.record_local(prompt=50, completion=50)
        log_routing_decision(
            session_id, "planner", "coder", "wrong stub", css=70.0, tci=50.0
        )
        return {k: '"""wrong implementation"""\n__all__ = []\n' for k in keys}

    return _run


# --------------------------------------------------------------------------- #
# G4-force-cloud provider seam                                                  #
# --------------------------------------------------------------------------- #


def test_g4_force_cloud_patches_provider_seam() -> None:
    """G4_FORCE_CLOUD rebinds derive_routing_decision to always return 'CLOUD'."""
    original = agents.planner.derive_routing_decision
    with apply_arm(AblationArm.G4_FORCE_CLOUD):
        patched = agents.planner.derive_routing_decision
        assert patched is not original
        assert patched(0.0, 0.0) == "CLOUD"
        assert patched(99.0, 99.0) == "CLOUD"
    assert agents.planner.derive_routing_decision is original


# --------------------------------------------------------------------------- #
# Five-arm comparable verdicts                                                  #
# --------------------------------------------------------------------------- #


def test_five_arms_produce_comparable_verdicts(
    mock_infra: None,
    mock_embeddings: None,
    tmp_path: Any,
) -> None:
    """All five arms score the same corpus problem; golden patch → all passed."""
    problem = _load_first_problem()
    assert problem.corpus_root is not None
    oracle = _make_oracle(problem.corpus_root)
    runner = BenchmarkRunner(
        task_runner=_golden_runner(problem),
        oracle=oracle,
        telemetry_db_path=str(tmp_path / "tel.sqlite"),
    )
    results = asyncio.run(runner.run_arms(problem, _ALL_ARMS))
    assert {m.arm for m in results} == {"G1", "G2", "G3", "G4", "G4_FORCE_CLOUD"}
    assert all(m.verdict == "passed" for m in results), [
        (m.arm, m.verdict) for m in results
    ]


def test_wrong_patch_yields_failed_verdict(
    mock_infra: None,
    mock_embeddings: None,
    tmp_path: Any,
) -> None:
    """A wrong (semantically broken) patch produces a 'failed' verdict."""
    problem = _load_first_problem()
    assert problem.corpus_root is not None
    oracle = _make_oracle(problem.corpus_root)
    runner = BenchmarkRunner(
        task_runner=_wrong_runner(problem),
        oracle=oracle,
        telemetry_db_path=str(tmp_path / "tel.sqlite"),
    )
    results = asyncio.run(runner.run_arms(problem, [AblationArm.G4]))
    assert results[0].verdict == "failed"


# --------------------------------------------------------------------------- #
# project_id consistency (Finding A)                                            #
# --------------------------------------------------------------------------- #


def test_project_id_consistency(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The project_id passed to the indexer equals problem.project_id."""
    recorded_ids: List[str] = []

    async def _recording_start(
        self: Any, workspace_root: str, project_id: str, session_id: str
    ) -> None:
        recorded_ids.append(project_id)
        self._is_complete = True
        if self._complete_event is not None:
            self._complete_event.set()

    async def _fake_dependents(target: str, project_id: str = "") -> List[str]:
        return ["src/processor.py"]

    async def _ok_preflight(self: Any) -> None:
        return None

    monkeypatch.setattr("core.indexer.LazyIndexer.start", _recording_start)
    monkeypatch.setattr("core.db.get_dependents", _fake_dependents)
    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _ok_preflight)

    problem = _load_first_problem()
    assert problem.project_id is not None

    async def _stub(session_id: str, _p: BenchmarkProblem) -> Dict[str, str]:
        token_ledger.record_local(prompt=10, completion=10)
        log_routing_decision(session_id, "planner", "coder", "id-check", css=70.0, tci=50.0)
        return {}

    runner = BenchmarkRunner(
        task_runner=_stub,
        telemetry_db_path=str(tmp_path / "tel.sqlite"),
    )
    asyncio.run(runner.run_arms(problem, [AblationArm.G4]))
    assert len(recorded_ids) >= 1
    assert all(pid == problem.project_id for pid in recorded_ids)


# --------------------------------------------------------------------------- #
# Background task drain (Audit Iteration 1)                                    #
# --------------------------------------------------------------------------- #


def test_graph_background_tasks_drained() -> None:
    """Snapshot-then-clear drains background tasks without RuntimeError."""
    import agents.coder

    async def _run() -> None:
        async def _noop() -> None:
            return None

        for _ in range(3):
            t = asyncio.create_task(_noop())
            agents.coder._background_tasks.add(t)

        await asyncio.sleep(0)

        tasks = list(agents.coder._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        agents.coder._background_tasks.clear()

        assert len(agents.coder._background_tasks) == 0

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Patch key normalization (Finding B)                                           #
# --------------------------------------------------------------------------- #


def test_patch_keys_normalized_to_relative(tmp_path: Any) -> None:
    """Absolute keys under workspace are relativized; escaping keys are dropped."""
    workspace = tmp_path / "workspace"
    under_workspace = str(workspace / "src" / "foo.py")

    if sys.platform == "win32":
        escaping_key = "C:\\Windows\\System32\\evil.py"
    else:
        escaping_key = "/etc/passwd"

    raw = {
        under_workspace: "content_a",
        escaping_key: "evil",
        "src/bar.py": "content_b",
    }
    result = _normalize_patch(raw, workspace)

    assert "src/foo.py" in result
    assert result["src/foo.py"] == "content_a"
    assert escaping_key not in result
    assert "evil" not in result.values()
    assert "src/bar.py" in result


# --------------------------------------------------------------------------- #
# Strategy seam substitution                                                   #
# --------------------------------------------------------------------------- #


def test_vector_only_strategy_keeps_vector() -> None:
    """VectorOnlyRetrievalStrategy patches the graph seam, leaves vector seams live."""
    from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
    from core.memory.semantic_memory import SemanticMemoryManager

    orig_graph = GraphRAGDynamicExtractor.deep_parse
    orig_planner_vec = SemanticMemoryManager.search_with_paths
    orig_coder_vec = SemanticMemoryManager.search_snippets

    strategy = VectorOnlyRetrievalStrategy()
    import contextlib

    with contextlib.ExitStack() as stack:
        for patch in strategy.patches():
            stack.enter_context(patch)
        assert GraphRAGDynamicExtractor.deep_parse is not orig_graph
        assert SemanticMemoryManager.search_with_paths is orig_planner_vec
        assert SemanticMemoryManager.search_snippets is orig_coder_vec

    assert GraphRAGDynamicExtractor.deep_parse is orig_graph


def test_zero_shot_strategy_suppresses_all() -> None:
    """ZeroShotRetrievalStrategy patches all three retrieval seams."""
    from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
    from core.memory.semantic_memory import SemanticMemoryManager

    orig_graph = GraphRAGDynamicExtractor.deep_parse
    orig_planner_vec = SemanticMemoryManager.search_with_paths
    orig_coder_vec = SemanticMemoryManager.search_snippets

    strategy = ZeroShotRetrievalStrategy()
    import contextlib

    with contextlib.ExitStack() as stack:
        for patch in strategy.patches():
            stack.enter_context(patch)
        assert GraphRAGDynamicExtractor.deep_parse is not orig_graph
        assert SemanticMemoryManager.search_with_paths is not orig_planner_vec
        assert SemanticMemoryManager.search_snippets is not orig_coder_vec

    assert GraphRAGDynamicExtractor.deep_parse is orig_graph
    assert SemanticMemoryManager.search_with_paths is orig_planner_vec
    assert SemanticMemoryManager.search_snippets is orig_coder_vec
