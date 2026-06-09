# ailienant-core/tests/test_task_service_apply.py
"""_run_coding_task approval → actuation gate.

The coding path drives the compiled LangGraph engine and then applies the
patches found in the FINAL graph state behind the HITL card. These tests patch
the engine at the ``astream`` seam — yielding a crafted final state — so the
control flow (summary → approval → apply) is exercised in isolation from the
agents' internals.

Approved ⇒ the write pipeline is invoked with the final state's content + base
hashes. Rejected ⇒ nothing is applied.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict
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


# The final graph state the engine is mocked to produce: a finished plan plus
# one coder step's merged patch/content/base-hash (the reducers would have
# unioned these across steps in a real run).
_FINAL_STATE: Dict[str, Any] = {
    "mission_spec": _mission(),
    "pending_patches": {"calc.py": "--- a/calc.py\n+++ b/calc.py\n"},
    "pending_contents": {"calc.py": "def f():\n    return 2\n"},
    "pending_base_hash": {"calc.py": "deadbeef"},
    "errors": [],
    "hitl_pending": False,
}


def _fake_astream(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
    """Stand in for ``alienant_app.astream(...)`` — yield one final snapshot."""

    async def _gen() -> AsyncIterator[Dict[str, Any]]:
        yield _FINAL_STATE

    return _gen()


def _common_patches(approved: bool, apply_mock: AsyncMock):
    decision = {"approved": approved, "comment": None, "modified_content": None}
    return [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
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
    assert apply_mock.await_args is not None
    session_id, contents, base_hashes = apply_mock.await_args.args[:3]
    assert session_id == "s1"
    assert contents == _FINAL_STATE["pending_contents"]
    assert base_hashes == _FINAL_STATE["pending_base_hash"]


@pytest.mark.anyio
async def test_hitl_request_carries_proposed_files() -> None:
    # The FILE_WRITE approval must ride the proposed post-edit content so the host
    # can render the inline diff atomically with the Accept/Reject row — one event,
    # no separate preview broadcast that could desync.
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    approval_mock = AsyncMock(return_value={"approved": True, "comment": None, "modified_content": None})
    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
    ]
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    approval_mock.assert_awaited_once()
    assert approval_mock.await_args is not None
    kwargs = approval_mock.await_args.kwargs
    assert kwargs["request_kind"] == "FILE_WRITE"
    proposed = kwargs["proposed_files"]
    assert proposed is not None and len(proposed) == 1
    pf = proposed[0]
    assert pf.file_path == "calc.py"
    assert pf.new_content == _FINAL_STATE["pending_contents"]["calc.py"]
    assert pf.base_hash == _FINAL_STATE["pending_base_hash"]["calc.py"]


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
