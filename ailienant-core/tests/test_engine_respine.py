# ailienant-core/tests/test_engine_respine.py
"""Engine Re-Spine — the live coding path drives the compiled LangGraph engine.

These tests certify the foundational backend correction: routing
``_run_coding_task`` through ``alienant_app`` re-arms the mode router, the
Socratic ideation loop, and the checkpointer in one move, and the WS planner
toggle reaches the graph via the submit endpoint.

The planner stub (``AILIENANT_PLANNER_DEBUG=1``) and the analyst Socratic stub
make the graph hermetic — no BYOM engine is contacted.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskService, TaskPayload
from brain.state import MissionSpecification, WBSStep


@pytest.fixture(autouse=True)
def _planner_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the deterministic planner stub so the graph never needs a model."""
    monkeypatch.setenv("AILIENANT_PLANNER_DEBUG", "1")


@pytest.fixture
def _l2_checkpoint(tmp_path: Any) -> Any:
    """Open an isolated L2 sqlite so promote() can persist; close on teardown."""
    from brain.checkpoint import checkpoint_manager

    prev_conn = checkpoint_manager._conn
    prev_path = checkpoint_manager.db_path
    checkpoint_manager.db_path = str(tmp_path / "respine_state.sqlite")
    checkpoint_manager.initialize()
    try:
        yield checkpoint_manager
    finally:
        checkpoint_manager.close()
        checkpoint_manager._conn = prev_conn
        checkpoint_manager.db_path = prev_path


def _payload(*, planner_mode: bool) -> TaskPayload:
    return TaskPayload(
        task_prompt="build a CSV exporter",
        dirty_buffers=[],
        explicit_mentions=[],
        attachments=[],
        planner_mode_active=planner_mode,
        workspace_root="",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Planner mode → Socratic ideation suspend (no plan yet, question asked)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_mode_enters_ideation_and_suspends() -> None:
    """planner_mode_active=True must route to the ideation loop: the analyst asks
    a question and the turn suspends WITHOUT a MissionSpecification or HITL card."""
    ts = TaskService()  # type: ignore[no-untyped-call]
    broadcast_token = AsyncMock()
    broadcast_stream_end = AsyncMock()
    request_human_approval = AsyncMock()

    # The analyst imports vfs_manager locally, so patch it on the source module.
    with patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end), \
         patch("core.task_service.vfs_manager.request_human_approval", request_human_approval), \
         patch("api.websocket_manager.vfs_manager.broadcast_token", broadcast_token):
        await ts._run_coding_task("sess-ideation", _payload(planner_mode=True), "SEQUENTIAL")
        # The analyst broadcasts its question via a fire-and-forget task; let it run.
        await asyncio.sleep(0)

    # No approval card on a suspend — Ask/Plan never reached the write tier.
    request_human_approval.assert_not_awaited()
    # The stream was finalized so the UI's isStreaming flips back.
    broadcast_stream_end.assert_awaited()
    # A Socratic question went out to the user.
    assert broadcast_token.await_count >= 1
    asked = " ".join(str(c) for c in broadcast_token.await_args_list)
    assert "sess-ideation" in asked


# ──────────────────────────────────────────────────────────────────────────────
# 2. Non-planner: graph proposes patches → summary + HITL apply
#    (the apply-gate control flow lives in test_task_service_apply.py; here we
#     assert the graph's final-state patches reach the approval card.)
# ──────────────────────────────────────────────────────────────────────────────


_MISSION = MissionSpecification(
    outcome="Add a CSV exporter.",
    scope=["export.py"],
    constraints=["none"],
    decisions=["go"],
    tasks=[
        WBSStep(
            step_number=1,
            target_role="core_dev",
            action="edit_file",
            target_file="export.py",
            description="write exporter",
        )
    ],
    checks=["ok"],
)

_FINAL_WITH_PATCH: Dict[str, Any] = {
    "mission_spec": _MISSION,
    "pending_patches": {"export.py": "--- a/export.py\n+++ b/export.py\n"},
    "pending_contents": {"export.py": "def to_csv():\n    return ''\n"},
    "pending_base_hash": {"export.py": "cafef00d"},
    "errors": [],
    "hitl_pending": False,
}


def _astream_final(state: Dict[str, Any]) -> Any:
    def _maker(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield state

        return _gen()

    return _maker


@pytest.mark.anyio
async def test_non_planner_proposes_patches_and_requests_approval() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    request_human_approval = AsyncMock(
        return_value={"approved": True, "comment": None, "modified_content": None}
    )
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["export.py"]})

    with patch("brain.engine.alienant_app.astream", side_effect=_astream_final(_FINAL_WITH_PATCH)), \
         patch("core.write_pipeline.apply_patch_set", apply_mock), \
         patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.request_human_approval", request_human_approval):
        await ts._run_coding_task("sess-code", _payload(planner_mode=False), "SEQUENTIAL")

    request_human_approval.assert_awaited_once()
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == _FINAL_WITH_PATCH["pending_contents"]


# ──────────────────────────────────────────────────────────────────────────────
# 3. A completed graph run emits a checkpoint_id (⟲ Rewind affordance)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_completed_run_emits_checkpoint_id(_l2_checkpoint: Any) -> None:
    """Because the graph now runs on thread_id=session_id, _finalize_stream finds
    the L1 tuple, promotes it, and broadcasts a non-None checkpoint_id."""
    ts = TaskService()  # type: ignore[no-untyped-call]
    broadcast_stream_end = AsyncMock()

    with patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end), \
         patch("api.websocket_manager.vfs_manager.broadcast_token", new=AsyncMock()):
        # Planner mode suspends at the analyst (hermetic, no model) but still runs
        # ≥1 node on the thread → a checkpoint exists to promote.
        await ts._run_coding_task("sess-ckpt", _payload(planner_mode=True), "SEQUENTIAL")
        await asyncio.sleep(0)

    broadcast_stream_end.assert_awaited()
    cids = [c.kwargs.get("checkpoint_id") for c in broadcast_stream_end.await_args_list]
    assert any(cid is not None for cid in cids), (
        f"expected a non-None checkpoint_id in {broadcast_stream_end.await_args_list}"
    )
    # The promoted snapshot is discoverable in L2.
    assert len(_l2_checkpoint.list_checkpoints("sess-ckpt")) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# 4. Self-healing is owned by the graph, not the task service
# ──────────────────────────────────────────────────────────────────────────────


def test_task_service_no_longer_drives_self_heal() -> None:
    """The external attempt_correction loop was removed — self-healing now lives
    inside the graph (reflexion_guard → error_correction), and the planner/coder
    nodes are reached via the compiled graph, not imported directly. Guard against
    regressions that re-introduce the orphaned imports (checked at the import
    statements only — docstrings may still name the nodes for context)."""
    import re

    from pathlib import Path

    import core.task_service as ts_mod

    src = Path(ts_mod.__file__).read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    joined = "\n".join(import_lines)
    assert "run_planner_node" not in joined
    assert "run_coder_node" not in joined
    assert "attempt_correction" not in joined
    # The compiled graph IS imported (the new spine).
    assert any("alienant_app" in ln for ln in import_lines)


# ──────────────────────────────────────────────────────────────────────────────
# 5. The submit endpoint folds the WS planner-mode toggle into the payload
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_submit_reads_planner_mode_registry() -> None:
    """The toggle is stored per-session in planner_mode_registry; submit_task must
    fold it into the payload so the coding path can route to the ideation loop.
    Without this read the flag was always the default False."""
    import main

    captured: Dict[str, Any] = {}

    async def _capture(*, session_id: str, payload: TaskPayload, execution_mode: str) -> Dict[str, Any]:
        captured["planner_mode_active"] = payload.planner_mode_active
        return {"status": "success"}

    main.planner_mode_registry["sess-toggle"] = True
    body = TaskPayload(task_prompt="x", dirty_buffers=[], workspace_root="/ws")
    try:
        with patch.object(main.task_service, "process_task", side_effect=_capture), \
             patch("main._get_hw_profile", new=AsyncMock(return_value=type("H", (), {"suggested_mode": "SEQUENTIAL"})())), \
             patch("main.get_execution_mode_pref", return_value="SEQUENTIAL"):
            await main.submit_task(body, x_task_id="sess-toggle")
            # submit_task schedules the runner as a background task; drain it.
            for _ in range(50):
                if "planner_mode_active" in captured:
                    break
                await asyncio.sleep(0.01)
    finally:
        main.planner_mode_registry.pop("sess-toggle", None)

    assert captured.get("planner_mode_active") is True
