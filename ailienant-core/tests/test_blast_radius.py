"""Blast-radius mapper: resolved reverse-adjacency BFS over the dependency graph.

Exercises ``compute_blast_radius_sync`` directly against seeded edges/indexed-file
tuples (no live DB), plus the async fetch wrapper and the pre-apply gate wired into
``TaskService``:
- direct dependents, 3-hop transitive, cycle safety, and the empty graph,
- the resolved-adjacency crux: a TS/JS target is an extensionless specifier and a
  Python target is a dotted module — neither is the absolute file path a seed uses,
  so resolution (shared with confidence scoring, plus a fail-safe Python suffix
  index) is required for a dependent to be found at all,
- a relative diff-path seed still matches an absolute indexed file via the
  workspace-root join,
- the advisory edge-count cap,
- the task_service integration: an over-threshold radius escalates to human review
  and a decline vetoes the write; at/under threshold applies without prompting.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from core.blast_radius import (
    MAX_BLAST_EDGES,
    compute_blast_radius,
    compute_blast_radius_sync,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Core traversal: DoD rows ─────────────────────────────────────────────────


def test_direct_dependents() -> None:
    edges = (("b", "a"), ("c", "b"))
    indexed = ("a", "b", "c")
    assert compute_blast_radius_sync(("a",), edges, indexed, depth=1) == ["b"]


def test_three_hop_transitive() -> None:
    edges = (("b", "a"), ("c", "b"), ("d", "c"))
    indexed = ("a", "b", "c", "d")
    assert compute_blast_radius_sync(("a",), edges, indexed, depth=3) == ["b", "c", "d"]


def test_cycle_does_not_diverge() -> None:
    edges = (("b", "a"), ("c", "b"), ("a", "c"))
    indexed = ("a", "b", "c")
    assert compute_blast_radius_sync(("a",), edges, indexed, depth=3) == ["b", "c"]


def test_empty_graph() -> None:
    assert compute_blast_radius_sync(("a",), (), (), depth=3) == []


# ── Resolved-adjacency crux (the reason bfs_k_hop_backward can't be reused) ──


def test_ts_extensionless_target_resolves_to_dependent() -> None:
    edges = (("/ws/src/main.ts", "/ws/src/a"),)
    indexed = ("/ws/src/a.ts",)
    assert compute_blast_radius_sync(("/ws/src/a.ts",), edges, indexed) == ["/ws/src/main.ts"]


def test_ts_index_barrel_target_resolves() -> None:
    edges = (("/ws/src/main.ts", "/ws/src/widgets"),)
    indexed = ("/ws/src/widgets/index.ts",)
    assert compute_blast_radius_sync(
        ("/ws/src/widgets/index.ts",), edges, indexed
    ) == ["/ws/src/main.ts"]


def test_python_dotted_module_target_resolves() -> None:
    edges = (("/ws/pkg/app.py", "brain.state"),)
    indexed = ("/ws/pkg/brain/state.py", "/ws/pkg/app.py")
    assert compute_blast_radius_sync(
        ("/ws/pkg/brain/state.py",), edges, indexed
    ) == ["/ws/pkg/app.py"]


def test_python_dunder_init_module_target_resolves() -> None:
    edges = (("/ws/pkg/app.py", "brain"),)
    indexed = ("/ws/pkg/brain/__init__.py", "/ws/pkg/app.py")
    assert compute_blast_radius_sync(
        ("/ws/pkg/brain/__init__.py",), edges, indexed
    ) == ["/ws/pkg/app.py"]


def test_relative_seed_form_matches_absolute_indexed_file() -> None:
    edges = (("/ws/src/main.ts", "/ws/src/a"),)
    indexed = ("/ws/src/a.ts",)
    result = compute_blast_radius_sync(
        ("src/a.ts",), edges, indexed, workspace_root="/ws"
    )
    assert result == ["/ws/src/main.ts"]


def test_bare_specifier_target_has_no_dependents() -> None:
    # "react" never resolves to an indexed file, so it can seed no dependents —
    # it is an external module, correctly excluded from the reverse adjacency.
    edges = (("/ws/src/main.ts", "react"),)
    indexed = ("/ws/src/main.ts",)
    assert compute_blast_radius_sync(("react",), edges, indexed) == []


# ── Robustness ────────────────────────────────────────────────────────────────


def test_edge_cap_skips_the_check() -> None:
    edges = tuple((f"s{i}", f"t{i}") for i in range(MAX_BLAST_EDGES + 1))
    assert compute_blast_radius_sync(("t0",), edges, ("t0",)) == []


def test_non_string_seed_is_skipped_defensively() -> None:
    edges = (("b", "a"),)
    indexed = ("a", "b")
    # A malformed seed (e.g. an object, not a path string) must not raise —
    # a raised exception here would be swallowed by the caller's fail-open
    # handler and silently disable the gate for every legitimate seed too.
    assert compute_blast_radius_sync((None, "a"), edges, indexed) == ["b"]  # type: ignore[arg-type]


async def test_async_wrapper_fetches_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.blast_radius as br

    monkeypatch.setattr(
        br.catalog_db, "get_all_edges", AsyncMock(return_value=[("/ws/main.ts", "/ws/a")])
    )
    monkeypatch.setattr(
        br.catalog_db, "list_indexed_files", AsyncMock(return_value=["/ws/a.ts"])
    )
    result = await compute_blast_radius("proj", ["/ws/a.ts"])
    assert result == ["/ws/main.ts"]


# ── apply-gate integration (13.0.9) ──────────────────────────────────────────
#
# The blast-radius pre-apply gate now lives in brain/apply_gate.py's
# run_apply_prepare_node (escalation) and run_apply_commit_node (the actual
# interrupt + veto), gating one WBS step at a time instead of the whole turn's
# frozen pending_patches dict. These three tests used to drive
# TaskService._run_coding_task end-to-end via a faked alienant_app.astream —
# that seam no longer runs any apply logic at all (it moved into the graph),
# so the tests call the two nodes directly, mirroring test_task_service_apply.py.


def _mission() -> Any:
    from brain.state import MissionSpecification, WBSStep

    return MissionSpecification(
        outcome="Bump the increment.",
        scope=["calc.py"],
        constraints=["none"],
        decisions=["go"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="core_dev",
                action="edit_file",
                target_file="calc.py",
                description="bump",
            )
        ],
        checks=["ok"],
    )


def _state() -> Dict[str, Any]:
    return {
        "task_id": "s1",
        "project_id": "p1",
        "workspace_root": "/ws",
        "current_step_id": 1,
        "mission_spec": _mission(),
        "session_permission_mode": "STANDARD",  # ALLOW for WRITE, absent an escalation
        "pending_step_files": {"1": ["calc.py"]},
        "pending_step_command": {},
        "pending_contents": {"calc.py": "def f():\n    return 2\n"},
        "pending_base_hash": {"calc.py": "deadbeef"},
        "auto_accept_low_risk": False,
        "applied_files_log": [],
        "applied_step_ids": [],
        "apply_attempts": {},
    }


async def test_over_threshold_radius_escalates_and_veto_blocks_apply() -> None:
    from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node

    over_threshold = [f"dep{i}.py" for i in range(30)]
    with patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=over_threshold)):
        prepared = await run_apply_prepare_node(_state())

    assert prepared["pending_apply"]["decision"] == "hitl"  # escalated from ALLOW
    assert prepared["pending_apply"]["blast_radius_files"] == over_threshold
    assert prepared["mission_spec"].tasks[0].status == "awaiting_approval"

    state = _state()
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock()
    with patch("brain.apply_gate.request_graph_approval", return_value={"approved": False, "comment": None, "modified_content": None}), \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock):
        committed = await run_apply_commit_node(state)

    apply_mock.assert_not_awaited()  # declined escalation vetoes the write
    assert committed["mission_spec"].tasks[0].status == "rejected"


async def test_under_threshold_radius_does_not_escalate() -> None:
    from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node

    with patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=["dep.py"])):
        prepared = await run_apply_prepare_node(_state())

    assert prepared["pending_apply"]["decision"] == "allow"  # never escalated
    assert prepared["pending_apply"]["blast_radius_files"] == []

    state = _state()
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_not_called()  # under threshold, ALLOW never interrupts
    apply_mock.assert_awaited_once()
    assert committed["mission_spec"].tasks[0].status == "completed"


async def test_mapper_fault_fails_open_and_still_applies() -> None:
    from brain.apply_gate import run_apply_commit_node, run_apply_prepare_node

    with patch("core.blast_radius.compute_blast_radius", side_effect=RuntimeError("graph boom")), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        prepared = await run_apply_prepare_node(_state())

    # A mapper fault is advisory — it must never block a legitimate write, and
    # must not itself raise out of prepare.
    assert prepared["pending_apply"]["decision"] == "allow"

    state = _state()
    state["pending_apply"] = prepared["pending_apply"]
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    with patch("brain.apply_gate.request_graph_approval") as mock_interrupt, \
         patch("core.write_pipeline.apply_patch_set", new=apply_mock), \
         patch("core.task_service.run_patch_hooks", new=AsyncMock(return_value=(True, []))):
        committed = await run_apply_commit_node(state)

    mock_interrupt.assert_not_called()
    apply_mock.assert_awaited_once()
    assert committed["mission_spec"].tasks[0].status == "completed"
