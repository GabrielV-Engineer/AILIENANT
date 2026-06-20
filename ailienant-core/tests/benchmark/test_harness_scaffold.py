"""Hermetic gate for the benchmark harness scaffold.

Two layers keep the assertions honest:

* toggle correctness exercises the routing seams an arm patches (and proves they
  restore on exit) and the retrieval overrides an arm injects, independent of the
  pipeline, and
* runner plumbing drives the runner with a lightweight model-layer stub so the
  real token-delta / unique-session / canonical-row logic runs without a model.

The runner stays real for a manual live smoke; only the model layer is stubbed.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterator, List, Mapping

import pytest

import brain.engine as engine
import core.telemetry as telemetry
from core.telemetry import log_routing_decision
from core.token_ledger import token_ledger

from core.benchmark.arms import (
    ARM_TOGGLE_INVENTORY,
    AblationArm,
    apply_arm,
    retrieval_overrides_for,
)
from core.benchmark.hygiene import BenchmarkAbort
from core.benchmark.problems import DUMMY_PROBLEM, BenchmarkProblem
from core.benchmark.runner import BenchmarkRunner


@pytest.fixture(autouse=True)
def _restore_telemetry_conn() -> Iterator[None]:
    """Restore the telemetry connection so a tmp-DB run never leaks to later tests."""
    saved = telemetry._conn
    yield
    telemetry._conn = saved


# --- toggle correctness (real seams) -----------------------------------------


class _Step:
    def __init__(self, requires_iteration: bool) -> None:
        self.requires_iteration = requires_iteration


def test_g3_forces_one_shot_and_restores() -> None:
    step = _Step(requires_iteration=True)
    assert engine._coder_target(step) == "agentic_cell"  # baseline → ReAct cell
    with apply_arm(AblationArm.G3):
        assert engine._coder_target(step) == "coder_agent"  # forced one-shot
    assert engine._coder_target(step) == "agentic_cell"  # restored


def test_g2_injects_only_graph_override() -> None:
    """G2 degrades the graph via an injected override and leaves the vector seams live.

    Retrieval degradation is dependency-injected, not patched: the contract is the
    set of override keys, and apply_arm touches no retrieval symbol.
    """
    assert set(retrieval_overrides_for(AblationArm.G2)) == {"graph_fn"}
    with apply_arm(AblationArm.G2):
        pass  # no routing patch for a retrieval arm; nothing to assert on a symbol


def test_g1_injects_all_retrieval_overrides() -> None:
    """G1 injects overrides for the graph and both vector seams."""
    assert set(retrieval_overrides_for(AblationArm.G1)) == {
        "graph_fn",
        "planner_retrieval_fn",
        "coder_retrieval_fn",
    }


def test_inventory_covers_the_four_groups() -> None:
    for arm in (AblationArm.G1, AblationArm.G2, AblationArm.G3, AblationArm.G4):
        assert arm in ARM_TOGGLE_INVENTORY


# --- runner plumbing (stubbed model layer) -----------------------------------


async def _ok_preflight(self: Any) -> None:
    return None


def _make_stub(tokens_per_call: int = 100) -> Any:
    """A model-layer stub: records token usage and emits one scored routing row."""

    async def stub(
        session_id: str, problem: BenchmarkProblem, _overrides: Mapping[str, Any]
    ) -> Dict[str, str]:
        token_ledger.record_local(prompt=tokens_per_call, completion=tokens_per_call)
        log_routing_decision(
            session_id, "planner", "coder", "stub route", css=42.0, tci=63.0
        )
        return {}

    return stub


def _db_path(tmp_path: Any) -> str:
    return str(tmp_path / "telemetry.sqlite")


def test_four_groups_emit_raw_metrics(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _ok_preflight)
    runner = BenchmarkRunner(task_runner=_make_stub(), telemetry_db_path=_db_path(tmp_path))
    arms: List[AblationArm] = [
        AblationArm.G1,
        AblationArm.G2,
        AblationArm.G3,
        AblationArm.G4,
    ]
    results = asyncio.run(runner.run_arms(DUMMY_PROBLEM, arms))

    assert len(results) == 4
    assert {m.arm for m in results} == {"G1", "G2", "G3", "G4"}
    for metric in results:
        assert metric.tokens_local > 0  # raw token delta captured
        assert metric.tci == 63.0  # canonical TCI/CSS row selected per unique session
        assert metric.css == 42.0
        assert metric.verdict is None  # unscored: no oracle in the scaffold


def test_embeddings_down_aborts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(self: Any) -> str:
        return "embedding backend not reachable"

    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _down)
    runner = BenchmarkRunner(task_runner=_make_stub(), telemetry_db_path=_db_path(tmp_path))
    with pytest.raises(BenchmarkAbort):
        asyncio.run(runner.run_arms(DUMMY_PROBLEM, [AblationArm.G4]))


def test_cache_off_recomputes_and_never_resets_ledger(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.indexer.LazyIndexer._preflight_check", _ok_preflight)

    import core.response_cache as response_cache_mod

    clear_calls = {"n": 0}
    original_clear = response_cache_mod.response_cache.clear

    def counting_clear() -> None:
        clear_calls["n"] += 1
        original_clear()

    monkeypatch.setattr(response_cache_mod.response_cache, "clear", counting_clear)

    runner = BenchmarkRunner(task_runner=_make_stub(), telemetry_db_path=_db_path(tmp_path))

    before_total = token_ledger.snapshot()["local_tokens"]
    first = asyncio.run(runner.run_arms(DUMMY_PROBLEM, [AblationArm.G4]))
    second = asyncio.run(runner.run_arms(DUMMY_PROBLEM, [AblationArm.G4]))
    after_total = token_ledger.snapshot()["local_tokens"]

    # The same problem run twice recomputes tokens both times (no cache hit).
    assert first[0].tokens_local > 0
    assert second[0].tokens_local > 0
    # The cache is cleared before each problem.
    assert clear_calls["n"] >= 2
    # The global ledger accumulated both runs — it was never reset to zero.
    assert after_total >= before_total + 400
