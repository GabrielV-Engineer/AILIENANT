# ailienant-core/tests/test_task_service_apply.py
"""Phase 7.9.B.18 — _run_coding_task approval → actuation gate.

Approved ⇒ the write pipeline is invoked with the coder's content + base hashes.
Rejected ⇒ nothing is applied.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskService, TaskPayload
from brain.state import MissionSpecification, WBSStep


def _mission() -> MissionSpecification:
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


def _payload() -> TaskPayload:
    return TaskPayload(
        task_prompt="bump the increment",
        dirty_buffers=[],
        project_id=None,
        workspace_root="/ws",
    )


_CODER_RES = {
    "pending_patches": {"calc.py": "--- a/calc.py\n+++ b/calc.py\n"},
    "pending_contents": {"calc.py": "def f():\n    return 2\n"},
    "pending_base_hash": {"calc.py": "deadbeef"},
}


def _common_patches(approved: bool, apply_mock: AsyncMock):
    decision = {"approved": approved, "comment": None, "modified_content": None}
    return [
        patch("agents.planner.run_planner_node", new=AsyncMock(return_value={"mission_spec": _mission()})),
        patch("agents.coder.run_coder_node", new=AsyncMock(return_value=_CODER_RES)),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=AsyncMock(return_value=decision)),
    ]


@pytest.mark.anyio
async def test_approved_invokes_write_pipeline() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    ctxs = _common_patches(approved=True, apply_mock=apply_mock)
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    apply_mock.assert_awaited_once()
    session_id, contents, base_hashes = apply_mock.await_args.args[:3]
    assert session_id == "s1"
    assert contents == _CODER_RES["pending_contents"]
    assert base_hashes == _CODER_RES["pending_base_hash"]


@pytest.mark.anyio
async def test_rejected_does_not_apply() -> None:
    apply_mock = AsyncMock()
    ctxs = _common_patches(approved=False, apply_mock=apply_mock)
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    apply_mock.assert_not_awaited()
