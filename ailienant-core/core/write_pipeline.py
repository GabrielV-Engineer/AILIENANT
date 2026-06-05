# core/write_pipeline.py
"""Phase 7.9.B.18 — Enterprise Write Pipeline (VS Code applyEdit bridge).

Lean orchestrator: gate on a connected VS Code client, dispatch the approved
patch set to the extension host for vscode.workspace.applyEdit + save, and await
the host ack. The host owns the actual write and the stale-file guard; undo is
native Ctrl+Z / VS Code Local History.

By design this module performs NO filesystem I/O — no backups, no .bak, no atomic
disk writes, no headless fallback. If no client is connected we refuse the apply.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from api.ws_contracts import ApplyWorkspaceEditPayload, WorkspaceEditItem
from api.websocket_manager import vfs_manager

_ACK_TIMEOUT_S: float = 30.0


async def apply_patch_set(
    session_id: str,
    contents: Dict[str, str],
    base_hash: Dict[str, str],
    save: bool = True,
) -> Dict[str, Any]:
    """Dispatch an approved patch set to the VS Code host and await the apply ack.

    Returns the host's ack dict ({ok, applied_files, stale_files, error}) or an
    actionable error when no client is connected / the host doesn't respond.
    """
    if not contents:
        return {"ok": False, "error": "No changes to apply."}

    if not vfs_manager.has_client(session_id):
        return {"ok": False, "error": "No VS Code client connected to apply edits."}

    patch_id = uuid.uuid4().hex
    payload = ApplyWorkspaceEditPayload(
        patch_id=patch_id,
        save=save,
        edits=[
            WorkspaceEditItem(
                file_path=path,
                new_content=content,
                base_hash=base_hash.get(path),
            )
            for path, content in contents.items()
        ],
    )
    await vfs_manager.emit_apply_workspace_edit(session_id, payload)

    ack = await vfs_manager.wait_patch_ack(patch_id, timeout=_ACK_TIMEOUT_S)
    if ack is None:
        return {"ok": False, "error": "Timed out waiting for the editor to apply the change."}
    return ack
