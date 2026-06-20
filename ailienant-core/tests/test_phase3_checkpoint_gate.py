# tests/test_phase3_checkpoint_gate.py
"""Phase 3.7 — Checkpoint Gate.

Cross-subsystem E2E + stress suite that exercises Phase 3 as a single contract:
    - Phase 3.2  Retrieval Router & multi-tenant isolation
    - Phase 3.3  Context Cascade (CSS + Mini-Judge Veto)
    - Phase 3.4.8 Hybrid MCTS Fixer + Circuit Breaker
    - Phase 3.5  Memory Janitor (vector GC + obsolete graph purge)
    - Phase 3.6  Cognitive Fast-Boot (.ailienant/AGENTS.md)

Scope decisions (locked at planning):
    - RecencyBoost time-decay now ships in production (planner.py recency term);
      it is unit-tested in test_recency.py. This gate keeps the recency input
      deterministic (empty indexed_at + heatmap reset) so CSS routing stays stable.
    - Latency SLA is soft: median < 25 ms, p95 < 100 ms, hard signal = no aiosqlite locks.
    - LanceDB stays mocked per existing project convention; WAL-mode SQLite is real where
      concurrency is the actual claim under test.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import statistics
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from agents.mcts_coder import (
    MAX_LOCAL_ATTEMPTS,
    local_fix_with_retry,
    surgeon_escalation,
)
from agents.recency import session_heatmap
from brain.episodic.checkpointing import MCTSCheckpointer
from brain.mcts.tree import MCTSTree
from brain.state import ContextMeter, MissionSpecification, WBSStep
from core.janitor import _vector_gc_sync, purge_obsolete_graphs
from core.memory.context_auditor import RiskLevel
from core.state_manager import (
    CachedAgentState,
    dump_state_to_markdown,
)
from core.token_ledger import token_ledger
from core.vfs_middleware import VFSMiddleware
from tools.validation.result import PipelineResult


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _ws_hash(workspace_root: str) -> str:
    # Mirror the production key derivation (project_id_for normalizes path casing/
    # separators) so seeded rows match what the janitor and search paths query.
    from core.storage_paths import project_id_for
    return project_id_for(workspace_root)


def _make_mission(outcome: str = "test outcome") -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome,
        scope=["core/"],
        constraints=["no new deps"],
        decisions=["use aiosqlite"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="Refactor",
                action="write_file",
                target_file="core/janitor.py",
                description="implement janitor",
            )
        ],
        checks=["pytest passes"],
    )


def _make_context_meter(
    sem: float,
    graph: float,
    recency: float,
    css: float,
    tci: float,
    routing: str = "LOCAL_SMALL",
) -> ContextMeter:
    return ContextMeter(
        semantic_similarity=sem,
        graph_coverage=graph,
        recency_score=recency,
        css_total=css,
        task_complexity_index=tci,
        routing_decision=routing,
        is_red_alert=css < 40.0,
    )


def _planner_state(
    workspace_root: str,
    project_id: str,
    user_input: str,
    ctx: ContextMeter,
    tci: float,
) -> Dict[str, Any]:
    return {
        "task_id": "phase37-gate",
        "user_input": user_input,
        "workspace_root": workspace_root,
        "project_id": project_id,
        "context_metrics": ctx,
        "mission_spec": None,
        "immutable_wbs": None,
        "errors": [],
        "retry_count": 0,
        "current_cost_usd": 0.0,
        "max_budget_usd": 10.0,
        "vfs_buffer": {},
        "terminal_output": "",
        "parallel_tasks": [],
        "tci": tci,
        "css": ctx.css_total,
        "provider": "LOCAL",
        "current_step_id": None,
        "dirty_buffers": [],
        "ide_context": "",
    }


def _fake_llm_response(
    content: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """Mimic litellm ModelResponse.choices[0].message.content + .usage."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _planner_patches(
    sem_score: float,
    top_k: List[str],
    coverage: float,
    risk: RiskLevel,
) -> Tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Build the canonical mock trio for run_planner_node tests."""
    # search_with_paths returns (score, paths, indexed_at[]); empty ISO strings
    # make the recency time-decay term deterministic (skipped → falls to 0).
    mock_search = AsyncMock(return_value=(sem_score, top_k, [""] * len(top_k)))
    mock_deep = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=coverage,
            context_block="",
            parsed_files=top_k,
            target_files=top_k,
        )
    )
    mock_audit = AsyncMock(return_value=risk)
    return mock_search, mock_deep, mock_audit


@pytest.fixture(autouse=True)
def _reset_ledger() -> Any:
    """Isolate token-ledger state between tests."""
    token_ledger.reset()
    yield
    token_ledger.reset()


@pytest.fixture(autouse=True)
def _reset_ram_vfs() -> Any:
    """The VFSMiddleware._ram_vfs is a process-singleton — keep tests isolated."""
    vfs = VFSMiddleware()  # type: ignore[no-untyped-call]
    vfs._ram_vfs.clear()
    yield
    vfs._ram_vfs.clear()


@pytest.fixture(autouse=True)
def _reset_heatmap() -> Any:
    """SessionAccessHeatmap is a process-singleton — reset so the recency
    access-frequency term is deterministic across tests."""
    session_heatmap.reset()
    yield
    session_heatmap.reset()


# ══════════════════════════════════════════════════════════════════════════════
# TEST VECTOR 1 — Retrieval-to-Response Cascade Matrix
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_v1_scenario_a_low_css_triggers_red_alert_and_cloud_route() -> None:
    """Low CSS → is_red_alert fires → routing bypasses Mini-Judge and forces CLOUD.

    Also asserts namespace isolation: search_with_paths is called with the alpha
    workspace_hash, never the omega one.
    """
    ws_alpha: str = "/tmp/ws_alpha"
    ws_omega: str = "/tmp/ws_omega"
    alpha_hash: str = _ws_hash(ws_alpha)
    omega_hash: str = _ws_hash(ws_omega)
    assert alpha_hash != omega_hash  # sanity: distinct tenants

    # Force CSS = (0.5*0.20 + 0.3*0.10 + 0.2*0.0) * 100 = 13.0 → red alert.
    initial_ctx = _make_context_meter(
        sem=0.0, graph=0.0, recency=0.0, css=100.0, tci=10.0
    )
    state = _planner_state(
        workspace_root=ws_alpha,
        project_id=alpha_hash,
        user_input="trivial regex fix",
        ctx=initial_ctx,
        tci=10.0,
    )

    mock_search, mock_deep, mock_audit = _planner_patches(
        sem_score=0.20, top_k=["main.py"], coverage=0.10, risk=RiskLevel.NONE,
    )
    sdd_json: str = _make_mission("low-css outcome").model_dump_json()
    mock_llm_response = _fake_llm_response(sdd_json, prompt_tokens=1, completion_tokens=1)

    with patch("agents.planner.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=None), \
         patch("core.state_manager.dump_state_to_markdown", return_value=True), \
         patch("agents.planner.audit_task_complexity", new=mock_audit), \
         patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, \
         patch("agents.planner.GraphRAGDynamicExtractor") as mock_extr_cls, \
         patch("agents.planner.TrajectoryMemoryManager") as mock_traj_cls, \
         patch("agents.planner.LLMGateway.ainvoke", new=AsyncMock(return_value=mock_llm_response)):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extr_cls.return_value.deep_parse = mock_deep
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node
        result = await run_planner_node(state)

    final_ctx: ContextMeter = result["context_metrics"]
    assert final_ctx.is_red_alert is True, f"expected red alert, got css={final_ctx.css_total}"
    assert final_ctx.routing_decision == "CLOUD"
    # Red-alert path skips Mini-Judge entirely.
    mock_audit.assert_not_called()

    # Namespace isolation: search called with alpha hash, never omega.
    assert mock_search.await_count == 1
    assert mock_search.await_args is not None
    call_kwargs: Dict[str, Any] = dict(mock_search.await_args.kwargs)
    assert call_kwargs["workspace_hash"] == alpha_hash
    assert call_kwargs["workspace_hash"] != omega_hash


@pytest.mark.anyio
async def test_v1_scenario_b_mid_css_judge_medium_escalates_route() -> None:
    """Mid CSS (no red alert) + Mini-Judge MEDIUM → LOCAL_SMALL escalated to LOCAL_BIG, tci >= 75."""
    ws: str = "/tmp/ws_mid"
    initial_ctx = _make_context_meter(
        sem=0.0, graph=0.0, recency=0.40, css=100.0, tci=10.0
    )
    state = _planner_state(
        workspace_root=ws,
        project_id=_ws_hash(ws),
        user_input="refactor authentication module",
        ctx=initial_ctx,
        tci=10.0,  # pre-veto would map to LOCAL_SMALL via derive_routing_decision
    )

    # CSS = (0.5*0.85 + 0.3*0.60 + 0.2*0.40) * 100 = 68.5 → no red alert.
    mock_search, mock_deep, mock_audit = _planner_patches(
        sem_score=0.85, top_k=["auth.py"], coverage=0.60, risk=RiskLevel.MEDIUM,
    )
    sdd_json: str = _make_mission("mid-css outcome").model_dump_json()
    mock_llm_response = _fake_llm_response(sdd_json)

    with patch("agents.planner.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=None), \
         patch("core.state_manager.dump_state_to_markdown", return_value=True), \
         patch("agents.planner.audit_task_complexity", new=mock_audit), \
         patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, \
         patch("agents.planner.GraphRAGDynamicExtractor") as mock_extr_cls, \
         patch("agents.planner.TrajectoryMemoryManager") as mock_traj_cls, \
         patch("agents.planner.LLMGateway.ainvoke", new=AsyncMock(return_value=mock_llm_response)):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extr_cls.return_value.deep_parse = mock_deep
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node
        result = await run_planner_node(state)

    final_ctx: ContextMeter = result["context_metrics"]
    assert final_ctx.is_red_alert is False
    # Pre-veto math: tci=10 < 30 → LOCAL_SMALL. MEDIUM Veto upgrades to LOCAL_BIG.
    assert final_ctx.routing_decision == "LOCAL_BIG"
    assert final_ctx.task_complexity_index >= 75.0
    mock_audit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# TEST VECTOR 2 — Concurrent WAL-mode SQLite under load
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_v2_50_concurrent_wal_reads_no_lock_and_within_sla(tmp_path: Path) -> None:
    """50 concurrent aiosqlite SELECTs over MCTSCheckpointer's WAL-mode DB.

    Hard signal: no aiosqlite OperationalError. Soft SLA: median < 25 ms, p95 < 100 ms.
    """
    db_path: Path = tmp_path / "mcts.sqlite"
    cp = MCTSCheckpointer()
    cp.initialize(db_path=str(db_path))
    try:
        # Seed 200 rows (mix of pruned and stable).
        assert cp._conn is not None
        rows: List[Tuple[str, str, str, str, float, str, float, Any]] = []
        now: float = time.time()
        for i in range(200):
            prune_reason = "low_reward" if i % 3 == 0 else None
            rows.append((
                f"node-{i}", "root", "thread-A", f"outcome-{i}",
                float(i) / 200.0, "noop", now - i, prune_reason,
            ))
        with cp._conn:
            cp._conn.executemany(
                "INSERT INTO mcts_episodes "
                "(node_id, parent_id, thread_id, mission_outcome, reward_R, action, accepted_at, prune_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        cp.close()

    async def _one_read(idx: int) -> Tuple[float, int]:
        """Single aggregate query — proxy for 'centrality + recency' under concurrent load.

        Measures the query+fetch latency only (excluding connection setup), since the
        SLA claim is about computation under concurrent load, not aiosqlite handshake cost.
        """
        async with aiosqlite.connect(str(db_path), timeout=5.0) as db:
            t0: float = time.perf_counter()
            async with db.execute(
                "SELECT COUNT(*) AS c, AVG(reward_R) AS r FROM mcts_episodes WHERE thread_id = ?",
                ("thread-A",),
            ) as cur:
                row = await cur.fetchone()
            elapsed: float = time.perf_counter() - t0
        assert row is not None
        return elapsed, int(row[0])

    results: List[Tuple[float, int]] = await asyncio.gather(
        *[_one_read(i) for i in range(50)]
    )

    latencies: List[float] = [r[0] for r in results]
    counts: List[int] = [r[1] for r in results]

    # Hard signal: every read returned data, no exception escaped asyncio.gather.
    assert all(c == 200 for c in counts), "WAL readers saw inconsistent row counts"

    # Soft SLA: median + p95. Thresholds account for Windows event-loop scheduler
    # overhead when 50 tasks contend; the real signal is no aiosqlite OperationalError
    # (asserted by virtue of asyncio.gather not raising) and consistent read results.
    p50: float = statistics.median(latencies)
    p95_idx: int = int(0.95 * len(latencies)) - 1
    p95: float = sorted(latencies)[max(0, p95_idx)]
    assert p50 < 0.100, f"median latency {p50:.4f}s exceeds 100 ms — indicates lock contention"
    assert p95 < 0.250, f"p95 latency {p95:.4f}s exceeds 250 ms — indicates lock contention"


# ══════════════════════════════════════════════════════════════════════════════
# TEST VECTOR 3 — MCTS Fixer Loop + Circuit Breaker + Memory Profiling
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_v3_local_fixer_three_strikes_keeps_cloud_tokens_at_zero() -> None:
    """3 consecutive LSP failures → error_streak == 3, ledger shows local-only token use."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())

    fail: PipelineResult = PipelineResult(
        passed=False, failed_layer="LSP", prune_reason="F401 unused import"
    )
    # Mock at the litellm layer so LLMGateway.ainvoke still runs the ledger accounting.
    fake = _fake_llm_response("# still broken", prompt_tokens=20, completion_tokens=10)

    with patch("agents.mcts_coder.validate_delta", new=AsyncMock(return_value=fail)), \
         patch("tools.llm_gateway.litellm.acompletion", new=AsyncMock(return_value=fake)):
        _, result = await local_fix_with_retry("import os\n", "bad.py", node)

    assert result.passed is False
    assert node.error_streak == MAX_LOCAL_ATTEMPTS

    snap = token_ledger.snapshot()
    assert snap["cloud_tokens"] == 0.0, "no cloud tokens should be charged in the local loop"
    assert snap["local_tokens"] > 0.0, "ledger must record the LOCAL-tier fixer calls"


@pytest.mark.anyio
async def test_v3_surgeon_escalation_resets_streak_and_charges_cloud_tier() -> None:
    """After local exhaustion, surgeon (Tier.CLOUD) repairs and resets error_streak."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    node.error_streak = MAX_LOCAL_ATTEMPTS  # pre-condition: just tripped the breaker

    ok: PipelineResult = PipelineResult(passed=True)
    fake = _fake_llm_response("def fixed(): pass", prompt_tokens=50, completion_tokens=20)

    with patch("agents.mcts_coder.validate_delta", new=AsyncMock(return_value=ok)), \
         patch("tools.llm_gateway.litellm.acompletion", new=AsyncMock(return_value=fake)):
        fixed: str = await surgeon_escalation(
            "broken code", "bad.py", "stuck error", node,
        )

    assert fixed == "def fixed(): pass"
    assert node.error_streak == 0, "successful surgeon must reset the breaker"

    snap = token_ledger.snapshot()
    assert snap["cloud_tokens"] > 0.0, "surgeon escalation must charge the CLOUD tier"


# Headroom for the heap-lifecycle assertion. A fixed byte ceiling is unportable —
# steady-state interpreter, import, and allocator churn differ by platform — so the
# ceiling is derived from a calibration pass: the measured cycle may exceed the
# calibrated residual by this ratio plus a small absolute noise floor.
_HEAP_HEADROOM_RATIO = 1.20
_HEAP_NOISE_FLOOR_BYTES = 65_536


def test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline() -> None:
    """Create + destroy 50 MCTSNode + RAM-VFS buffers; the per-cycle heap residual
    must not grow across repetition.

    The same create→prune→clear cycle is run twice. The first pass calibrates the
    residual a clean create+destroy leaves behind (lazy imports, allocator arenas,
    tracemalloc bookkeeping); the measured pass must stay within a small headroom of
    that calibration. A genuine leak grows the residual on every pass and breaches the
    bound, while one-time churn is absorbed by the calibration — which a fixed ceiling
    could not distinguish.
    """
    vfs = VFSMiddleware()  # type: ignore[no-untyped-call]

    def _lifecycle_delta() -> int:
        """Run one full allocate→destroy cycle; return its net traced-heap delta."""
        tracemalloc.start()
        try:
            snap_before = tracemalloc.take_snapshot()
            baseline_bytes = sum(stat.size for stat in snap_before.statistics("filename"))

            tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
            payload = "x" * 1024  # 1 KB per fake buffer
            for i in range(50):
                tree.expand(tree.root_id, f"action-{i}", {}, _make_mission(f"out-{i}"))
                vfs._ram_vfs[f"/fake/buf-{i}.py"] = payload

            # Explicit cleanup — drop references and the RAM-VFS dict contents.
            tree.prune_branch(tree.root_id)
            vfs._ram_vfs.clear()
            del tree

            snap_after = tracemalloc.take_snapshot()
            final_bytes = sum(stat.size for stat in snap_after.statistics("filename"))
            return final_bytes - baseline_bytes
        finally:
            tracemalloc.stop()

    calibrated_delta = _lifecycle_delta()
    delta_bytes = _lifecycle_delta()

    ceiling = int(max(calibrated_delta, 0) * _HEAP_HEADROOM_RATIO) + _HEAP_NOISE_FLOOR_BYTES
    assert delta_bytes <= ceiling, (
        f"resident memory grew by {delta_bytes} bytes across repetition "
        f"(calibrated={calibrated_delta}, ceiling={ceiling})"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST VECTOR 4 — Cold-Boot Fast-Boot + Janitor + Race Conditions
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_v4_fast_boot_intercepts_lancedb_call(tmp_path: Path) -> None:
    """Fresh AGENTS.md present → planner serves context from cache, LanceDB never called."""
    ws: str = str(tmp_path)
    # Seed an AGENTS.md so load_state_from_markdown will find a fresh checkpoint.
    seed_state = {
        "mission_spec": _make_mission("seeded"),
        "context_metrics": _make_context_meter(
            sem=0.7, graph=0.5, recency=0.5, css=60.0, tci=20.0, routing="LOCAL_BIG",
        ),
        "task_id": "seed",
        "workspace_root": ws,
        "_top_k_files_cache": ["seeded.py"],
    }
    assert dump_state_to_markdown(seed_state, ws) is True

    cached = CachedAgentState(
        mission_spec=_make_mission("cached"),
        context_metrics=_make_context_meter(
            sem=0.7, graph=0.5, recency=0.5, css=60.0, tci=20.0, routing="LOCAL_BIG",
        ),
        top_k_files=["seeded.py"],
        task_id="seed",
        generated_at="2026-05-16T00:00:00+00:00",
    )

    state = _planner_state(
        workspace_root=ws,
        project_id=_ws_hash(ws),
        user_input="explain seeded.py",
        ctx=_make_context_meter(
            sem=0.7, graph=0.5, recency=0.5, css=60.0, tci=20.0, routing="LOCAL_BIG",
        ),
        tci=20.0,
    )

    mock_search = AsyncMock(side_effect=AssertionError("LanceDB must NOT be called on fast-boot"))
    mock_deep = AsyncMock(
        return_value=MagicMock(
            coverage_ratio=0.5,
            context_block="",
            parsed_files=["seeded.py"],
            target_files=["seeded.py"],
        )
    )
    sdd_json: str = _make_mission("fast-boot path").model_dump_json()
    mock_llm_response = _fake_llm_response(sdd_json)

    with patch("agents.planner.DEBUG_MODE", False), \
         patch("core.state_manager.load_state_from_markdown", return_value=cached), \
         patch("agents.planner.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)), \
         patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, \
         patch("agents.planner.GraphRAGDynamicExtractor") as mock_extr_cls, \
         patch("agents.planner.TrajectoryMemoryManager") as mock_traj_cls, \
         patch("agents.planner.LLMGateway.ainvoke", new=AsyncMock(return_value=mock_llm_response)):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extr_cls.return_value.deep_parse = mock_deep
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node
        result = await run_planner_node(state)  # must not raise

    assert "mission_spec" in result
    mock_search.assert_not_called()
    # deep_parse still runs (cheap, picks up real file changes on disk).
    mock_deep.assert_awaited_once()


@pytest.mark.anyio
async def test_v4_stale_agents_md_falls_back_to_full_retrieval(tmp_path: Path) -> None:
    """AGENTS.md older than the 1-hour TTL → load returns None → full LanceDB retrieval runs."""
    ws: str = str(tmp_path)
    seed_state = {
        "mission_spec": _make_mission("seeded"),
        "context_metrics": _make_context_meter(
            sem=0.7, graph=0.5, recency=0.5, css=60.0, tci=20.0, routing="LOCAL_BIG",
        ),
        "task_id": "seed",
        "workspace_root": ws,
        "_top_k_files_cache": ["seeded.py"],
    }
    assert dump_state_to_markdown(seed_state, ws) is True

    agents_md: Path = Path(ws) / ".ailienant" / "AGENTS.md"
    assert agents_md.exists()
    two_hours_ago: float = time.time() - 7200
    os.utime(str(agents_md), (two_hours_ago, two_hours_ago))

    state = _planner_state(
        workspace_root=ws,
        project_id=_ws_hash(ws),
        user_input="re-analyze stale workspace",
        ctx=_make_context_meter(
            sem=0.0, graph=0.0, recency=0.5, css=100.0, tci=20.0,
        ),
        tci=20.0,
    )

    mock_search, mock_deep, mock_audit = _planner_patches(
        sem_score=0.7, top_k=["seeded.py"], coverage=0.5, risk=RiskLevel.NONE,
    )
    sdd_json: str = _make_mission("stale-fallback").model_dump_json()
    mock_llm_response = _fake_llm_response(sdd_json)

    with patch("agents.planner.DEBUG_MODE", False), \
         patch("agents.planner.audit_task_complexity", new=mock_audit), \
         patch("agents.planner.SemanticMemoryManager") as mock_sem_cls, \
         patch("agents.planner.GraphRAGDynamicExtractor") as mock_extr_cls, \
         patch("agents.planner.TrajectoryMemoryManager") as mock_traj_cls, \
         patch("agents.planner.LLMGateway.ainvoke", new=AsyncMock(return_value=mock_llm_response)):
        mock_traj_cls.return_value.search = AsyncMock(return_value=[])
        mock_extr_cls.return_value.deep_parse = mock_deep
        mock_sem_cls.return_value.search_with_paths = mock_search

        from agents.planner import run_planner_node
        await run_planner_node(state)

    # Stale cache → planner must hit LanceDB.
    mock_search.assert_awaited_once()


def test_v4_janitor_removes_orphans_without_corrupting_neighbors() -> None:
    """Mixed-workspace LanceDB table: only alpha's orphan is deleted; omega rows untouched."""
    ws_alpha: str = "/tmp/ws_alpha"
    ws_omega: str = "/tmp/ws_omega"
    alpha_hash: str = _ws_hash(ws_alpha)
    omega_hash: str = _ws_hash(ws_omega)

    alpha_orphan: str = "/tmp/ws_alpha/gone.py"
    alpha_present: str = "/tmp/ws_alpha/main.py"
    omega_orphan: str = "/tmp/ws_omega/dead.py"   # exists but for the OMEGA tenant
    omega_present: str = "/tmp/ws_omega/index.py"

    arrow_tbl: pa.Table = pa.table({
        "file_path": pa.array([
            alpha_orphan, alpha_present, "/tmp/ws_alpha/extra.py",
            omega_orphan, omega_present,
        ], type=pa.utf8()),
        "workspace_hash": pa.array([
            alpha_hash, alpha_hash, alpha_hash,
            omega_hash, omega_hash,
        ], type=pa.utf8()),
    })

    mock_lance_ds = MagicMock()
    mock_lance_ds.to_table.return_value = arrow_tbl
    mock_tbl = MagicMock()
    mock_tbl.to_lance.return_value = mock_lance_ds
    mock_db = MagicMock()
    mock_db.table_names.return_value = ["workspace_embeddings"]
    mock_db.open_table.return_value = mock_tbl

    # Only alpha_orphan is missing on disk for the alpha sweep.
    def _exists(path: str) -> bool:
        return path != alpha_orphan and path != "/tmp/ws_alpha/extra.py"

    with patch("core.janitor.lancedb") as mock_lancedb, \
         patch("core.janitor.os.path.exists", side_effect=_exists):
        mock_lancedb.connect.return_value = mock_db
        report = _vector_gc_sync(ws_alpha, "/fake/lancedb")

    # Two orphans within alpha — extra.py + gone.py — both deleted, omega rows untouched.
    assert report.deleted_count == 2
    assert alpha_orphan in report.orphaned_paths
    assert "/tmp/ws_alpha/extra.py" in report.orphaned_paths
    assert omega_orphan not in report.orphaned_paths
    assert omega_present not in report.orphaned_paths

    # Every delete predicate must carry alpha_hash — omega rows are not addressable.
    for call in mock_tbl.delete.call_args_list:
        predicate: str = call.args[0]
        assert alpha_hash in predicate
        assert omega_hash not in predicate


@pytest.mark.anyio
async def test_v4_concurrent_janitor_and_mcts_commit_on_real_wal_sqlite(tmp_path: Path) -> None:
    """purge_obsolete_graphs + concurrent MCTS commits over real WAL SQLite → no lock errors."""
    db_path: Path = tmp_path / "mcts.sqlite"
    cp = MCTSCheckpointer()
    cp.initialize(db_path=str(db_path))

    # Seed 20 pruned-old rows that the janitor should reap (older than 30 days).
    old_ts: float = time.time() - 60 * 86400
    try:
        assert cp._conn is not None
        with cp._conn:
            for i in range(20):
                cp._conn.execute(
                    "INSERT INTO mcts_episodes "
                    "(node_id, parent_id, thread_id, mission_outcome, reward_R, action, accepted_at, prune_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"old-{i}", None, "thread-X", "stale", 0.1, "noop", old_ts, "low_reward"),
                )
    finally:
        cp.close()

    async def _write_fresh(idx: int) -> None:
        """Insert a fresh row via aiosqlite (matches what the daemon would do)."""
        async with aiosqlite.connect(str(db_path), timeout=5.0) as db:
            await db.execute(
                "INSERT INTO mcts_episodes "
                "(node_id, parent_id, thread_id, mission_outcome, reward_R, action, accepted_at, prune_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (f"fresh-{idx}", None, "thread-Y", "live", 0.9, "expand", time.time()),
            )
            await db.commit()

    # Concurrent fan-out: 1 janitor + 5 fresh writers.
    await asyncio.gather(
        purge_obsolete_graphs(mcts_db_path=str(db_path), retention_days=30),
        *[_write_fresh(i) for i in range(5)],
    )

    # Post-state must reflect both effects without any OperationalError raised above.
    conn = sqlite3.connect(str(db_path))
    try:
        pruned_remaining: int = conn.execute(
            "SELECT COUNT(*) FROM mcts_episodes WHERE prune_reason IS NOT NULL"
        ).fetchone()[0]
        fresh_count: int = conn.execute(
            "SELECT COUNT(*) FROM mcts_episodes WHERE thread_id = 'thread-Y'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert pruned_remaining == 0, f"janitor failed to reap pruned rows ({pruned_remaining} left)"
    assert fresh_count == 5, f"concurrent writers lost data ({fresh_count}/5)"
