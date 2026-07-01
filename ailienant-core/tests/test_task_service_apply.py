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

from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskService, TaskPayload, _HOOK_TIMEOUT_SEC
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

    # The blast-radius check (stubbed empty by the autouse fixture) does not call
    # request_human_approval — only the per-file FILE_WRITE card does.
    approval_mock.assert_awaited_once()
    assert approval_mock.await_args is not None
    kwargs = approval_mock.await_args.kwargs
    assert kwargs["request_kind"] == "FILE_WRITE"
    proposed = kwargs["proposed_files"]
    assert proposed is not None and len(proposed) == 1
    pf = proposed[0]
    assert pf.file_path == "calc.py"
    # DEBT-024: the server ships an O(Δ) unified diff, not the full content.
    assert pf.new_content is None
    assert pf.unified_diff
    # No VFS buffer exists for calc.py in this test, so the old side is empty and
    # the whole new body arrives as added (+) lines.
    added = "".join(
        ln[1:]
        for ln in pf.unified_diff.splitlines(keepends=True)
        if ln.startswith("+") and not ln.startswith("+++")
    )
    assert added == _FINAL_STATE["pending_contents"]["calc.py"]
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


# ──────────────────────────────────────────────────────────────────────────────
# Strictly sequential multi-file approval: each file is its own decision.
# ──────────────────────────────────────────────────────────────────────────────

_TWO_FILE_STATE: Dict[str, Any] = {
    "mission_spec": _mission(),
    "pending_patches": {
        "a.py": "--- a/a.py\n+++ b/a.py\n",
        "b.py": "--- a/b.py\n+++ b/b.py\n",
    },
    "pending_contents": {"a.py": "A\n", "b.py": "B\n"},
    "pending_base_hash": {"a.py": "h1", "b.py": "h2"},
    "errors": [],
    "hitl_pending": False,
}


def _fake_astream_two(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
    async def _gen() -> AsyncIterator[Dict[str, Any]]:
        yield _TWO_FILE_STATE
    return _gen()


def _multi_patches(approval_mock: AsyncMock, apply_mock: AsyncMock):
    return [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream_two),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.request_human_approval", new=approval_mock),
    ]


@pytest.mark.anyio
async def test_sequential_accept_first_reject_second_applies_only_first() -> None:
    """The P3 regression: rejecting file #2 must NOT discard the accepted file #1."""
    # One approval per file, in order: accept a.py, reject b.py.
    approval_mock = AsyncMock(side_effect=[
        {"approved": True, "comment": None, "modified_content": None},
        {"approved": False, "comment": None, "modified_content": None},
    ])
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["a.py"], "stale_files": []})
    ctxs = _multi_patches(approval_mock, apply_mock)
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    # One approval per file (strictly sequential).
    assert approval_mock.await_count == 2
    # Only the accepted file reaches the write pipeline.
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == {"a.py": "A\n"}


@pytest.mark.anyio
async def test_sequential_all_rejected_applies_nothing() -> None:
    approval_mock = AsyncMock(side_effect=[
        {"approved": False, "comment": None, "modified_content": None},
        {"approved": False, "comment": None, "modified_content": None},
    ])
    apply_mock = AsyncMock()
    ctxs = _multi_patches(approval_mock, apply_mock)
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    assert approval_mock.await_count == 2
    apply_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_sequential_per_file_modified_content_honored() -> None:
    """Edit-before-apply on a single file overrides only that file's content."""
    approval_mock = AsyncMock(side_effect=[
        {"approved": True, "comment": None, "modified_content": "EDITED-A\n"},
        {"approved": True, "comment": None, "modified_content": None},
    ])
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["a.py", "b.py"], "stale_files": []})
    ctxs = _multi_patches(approval_mock, apply_mock)
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    _sid, contents, _bh = apply_mock.await_args.args[:3]
    assert contents == {"a.py": "EDITED-A\n", "b.py": "B\n"}


# ──────────────────────────────────────────────────────────────────────────────
# DEBT-028 — pre_patch / post_patch hooks execute around the single apply commit.
# ──────────────────────────────────────────────────────────────────────────────


class _FakeAdapter:
    """Records dispatched commands and returns a canned exit code."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.calls: List[Dict[str, Any]] = []

    async def execute(
        self, command: str, *, timeout_s: float, cwd: str, env_whitelist: Dict[str, str],
        session_id: Any = None,
    ) -> Any:
        self.calls.append({"command": command, "timeout_s": timeout_s})
        return SimpleNamespace(exit_code=self.exit_code, stdout="out", stderr="err")


def _hook_patches(
    apply_mock: AsyncMock,
    hook_rows: List[Dict[str, Any]],
    adapter: Optional[_FakeAdapter],
):
    decision = {"approved": True, "comment": None, "modified_content": None}
    return [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch(
            "core.task_service.vfs_manager.request_human_approval",
            new=AsyncMock(return_value=decision),
        ),
        patch("core.db.list_hooks", new=AsyncMock(return_value=hook_rows)),
        patch("core.sandbox.get_active_adapter", return_value=adapter),
        patch("tools.execution_tools._sandbox_env", return_value={}),
    ]


async def _run_with(ctxs) -> None:
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", _payload(), "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()


@pytest.mark.anyio
async def test_pre_patch_nonzero_vetoes_apply() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    adapter = _FakeAdapter(exit_code=1)
    rows = [{"event": "pre_patch", "command": "ruff check .", "enabled": 1}]
    await _run_with(_hook_patches(apply_mock, rows, adapter))

    apply_mock.assert_not_awaited()  # veto — the write never reaches disk
    # The ceiling is delegated to the adapter (no outer wait_for that would orphan).
    assert adapter.calls and adapter.calls[0]["timeout_s"] == _HOOK_TIMEOUT_SEC


@pytest.mark.anyio
async def test_pre_patch_adapter_timeout_vetoes_apply() -> None:
    # The adapter reaps its child and returns exit_code=-1 on its internal timeout;
    # a pre_patch gate that cannot complete must fail closed.
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    adapter = _FakeAdapter(exit_code=-1)
    rows = [{"event": "pre_patch", "command": "sleep 999", "enabled": 1}]
    await _run_with(_hook_patches(apply_mock, rows, adapter))

    apply_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_pre_patch_no_adapter_vetoes_apply() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    rows = [{"event": "pre_patch", "command": "ruff check .", "enabled": 1}]
    await _run_with(_hook_patches(apply_mock, rows, adapter=None))

    apply_mock.assert_not_awaited()  # fail-closed: gate cannot run → no write


@pytest.mark.anyio
async def test_post_patch_nonzero_only_warns() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    adapter = _FakeAdapter(exit_code=1)
    rows = [{"event": "post_patch", "command": "notify-fail", "enabled": 1}]
    await _run_with(_hook_patches(apply_mock, rows, adapter))

    apply_mock.assert_awaited_once()  # post hook failure is advisory — write landed
    assert adapter.calls and adapter.calls[0]["command"] == "notify-fail"


@pytest.mark.anyio
async def test_disabled_and_other_event_hooks_are_skipped() -> None:
    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"]})
    adapter = _FakeAdapter(exit_code=1)  # would veto IF it ran
    rows = [
        {"event": "pre_patch", "command": "would-fail", "enabled": 0},   # disabled
        {"event": "on_save", "command": "unrelated", "enabled": 1},       # other event
    ]
    await _run_with(_hook_patches(apply_mock, rows, adapter))

    apply_mock.assert_awaited_once()  # neither hook matched an active pre_patch gate
    assert adapter.calls == []
