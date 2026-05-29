# ailienant-core/tests/test_write_pipeline.py
"""Phase 7.9.B.18 — write pipeline orchestrator (no fs; pure WS bridge).

apply_patch_set gates on a connected VS Code client, dispatches the edit, and
awaits the host ack. It must never touch the filesystem: with no client it
returns an actionable error; with a client it returns the host's ack verbatim.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core import write_pipeline


_CONTENTS = {"calc.py": "def f():\n    return 2\n"}
_HASHES = {"calc.py": "abc123"}


@pytest.mark.anyio
async def test_no_client_returns_actionable_error() -> None:
    with patch.object(write_pipeline.vfs_manager, "has_client", return_value=False):
        res = await write_pipeline.apply_patch_set("s1", _CONTENTS, _HASHES)
    assert res == {"ok": False, "error": "No VS Code client connected to apply edits."}


@pytest.mark.anyio
async def test_empty_contents_short_circuits() -> None:
    # No edits → no client probe, no dispatch.
    res = await write_pipeline.apply_patch_set("s1", {}, {})
    assert res["ok"] is False
    assert "No changes" in res["error"]


@pytest.mark.anyio
async def test_dispatch_and_ack_ok() -> None:
    ack = {"patch_id": "p", "ok": True, "applied_files": ["calc.py"], "stale_files": []}
    with patch.object(write_pipeline.vfs_manager, "has_client", return_value=True), \
         patch.object(
             write_pipeline.vfs_manager, "emit_apply_workspace_edit", new=AsyncMock()
         ) as emit, \
         patch.object(
             write_pipeline.vfs_manager, "wait_patch_ack", new=AsyncMock(return_value=ack)
         ):
        res = await write_pipeline.apply_patch_set("s1", _CONTENTS, _HASHES)
    assert res == ack
    emit.assert_awaited_once()
    assert emit.await_args is not None
    payload = emit.await_args.args[1]
    assert payload.edits[0].file_path == "calc.py"
    assert payload.edits[0].base_hash == "abc123"


@pytest.mark.anyio
async def test_ack_timeout_returns_error() -> None:
    with patch.object(write_pipeline.vfs_manager, "has_client", return_value=True), \
         patch.object(write_pipeline.vfs_manager, "emit_apply_workspace_edit", new=AsyncMock()), \
         patch.object(write_pipeline.vfs_manager, "wait_patch_ack", new=AsyncMock(return_value=None)):
        res = await write_pipeline.apply_patch_set("s1", _CONTENTS, _HASHES)
    assert res["ok"] is False
    assert "Timed out" in res["error"]
