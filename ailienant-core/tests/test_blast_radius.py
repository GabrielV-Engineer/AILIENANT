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

from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock, patch

import pytest

from core.blast_radius import (
    DEFAULT_DEPTH,
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


# ── task_service integration (additive, boy-scout) ───────────────────────────


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


def _final_state() -> Dict[str, Any]:
    return {
        "mission_spec": _mission(),
        "pending_patches": {"calc.py": "--- a/calc.py\n+++ b/calc.py\n"},
        "pending_contents": {"calc.py": "def f():\n    return 2\n"},
        "pending_base_hash": {"calc.py": "deadbeef"},
        "errors": [],
        "hitl_pending": False,
    }


def _fake_astream(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
    async def _gen() -> AsyncIterator[Dict[str, Any]]:
        yield _final_state()

    return _gen()


def _payload() -> Any:
    from core.task_service import TaskPayload

    return TaskPayload(
        task_prompt="bump the increment",
        dirty_buffers=[],
        project_id=None,
        workspace_root="/ws",
    )


async def test_over_threshold_radius_escalates_and_veto_blocks_apply() -> None:
    from core.task_service import TaskService

    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    over_threshold = [f"dep{i}.py" for i in range(30)]

    # This payload's default permission mode may route through the per-file
    # FILE_WRITE HITL card before the blast-radius gate; approve that one (so the
    # flow actually reaches the gate) and decline only the BLAST_RADIUS escalation —
    # the two request kinds share one mocked function, so they're disambiguated by
    # the call's own request_kind rather than a single fixed return value.
    async def _approval_side_effect(*_a: Any, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("request_kind") == "BLAST_RADIUS":
            return {"approved": False, "comment": None}
        return {"approved": True, "comment": None, "modified_content": None}

    approval_mock = AsyncMock(side_effect=_approval_side_effect)
    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
        patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=over_threshold)),
    ]
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    blast_calls = [
        c for c in approval_mock.await_args_list if c.kwargs.get("request_kind") == "BLAST_RADIUS"
    ]
    assert len(blast_calls) == 1
    assert str(len(over_threshold)) in blast_calls[0].kwargs["action_description"]
    apply_mock.assert_not_awaited()  # declined escalation vetoes the write


async def test_under_threshold_radius_does_not_prompt() -> None:
    from core.task_service import TaskService

    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    approval_mock = AsyncMock(return_value={"approved": True, "comment": None, "modified_content": None})
    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
        patch("core.blast_radius.compute_blast_radius", new=AsyncMock(return_value=["dep.py"])),
    ]
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    # Under threshold: no BLAST_RADIUS escalation call at all — only the
    # per-file HITL approval fires (this payload's default permission mode).
    for call in approval_mock.await_args_list:
        assert call.kwargs.get("request_kind") != "BLAST_RADIUS"
    apply_mock.assert_awaited_once()


async def test_mapper_fault_fails_open_and_still_applies() -> None:
    from core.task_service import TaskService

    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    approval_mock = AsyncMock(return_value={"approved": True, "comment": None, "modified_content": None})
    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
        patch("core.blast_radius.compute_blast_radius", side_effect=RuntimeError("graph boom")),
    ]
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    # A mapper fault is advisory — it must never block a legitimate write.
    apply_mock.assert_awaited_once()
