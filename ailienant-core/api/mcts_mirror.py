# api/mcts_mirror.py
"""Phase 3.4.5 — Mirror service: expose MCTS parallel universes to the IDE.

Public API:
    get_virtual_file(node_id, path) -> Optional[str]
    apply_merge(node_id, workspace_root) -> MergeReport

These functions are framework-agnostic (no FastAPI imports) so the same code
is exercised by both pytest service tests and the HTTP route handlers in
main.py.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from brain.episodic.checkpointing import mcts_checkpointer
from brain.mcts.registry import get_tree_by_node
from core.blob_storage import blob_storage
from tools.validation.virtual_doc import VirtualDocumentProvider

logger = logging.getLogger("MCTS_MIRROR")


class MergeReport(BaseModel):
    """Outcome of an apply_merge() call. Serialised back to VS Code as JSON."""

    success: bool
    merged_files: int = 0
    workspace_root: str
    errors: List[str] = Field(default_factory=list)
    prune_count: int = 0


def get_virtual_file(node_id: str, path: str) -> Optional[str]:
    """Return content for `path` under MCTS `node_id`; None if unknown."""
    tree = get_tree_by_node(node_id)
    if tree is None:
        logger.info("get_virtual_file: unknown node %s..", node_id[:8])
        return None
    try:
        node = tree.get_node(node_id)
    except KeyError:
        logger.info("get_virtual_file: node %s.. exists in registry but not in tree", node_id[:8])
        return None
    return VirtualDocumentProvider(node.vfs_view).read(path)


def apply_merge(node_id: str, workspace_root: str) -> MergeReport:
    """Write a stable node's vfs_view to disk atomically; prune the branch.

    Safety guarantees:
      1. Sandbox: every path must resolve inside workspace_root.
      2. Preflight: ALL CAS blobs must be retrievable BEFORE the first disk
         write. Any miss aborts the operation without touching disk.
      3. Atomic per-file: tempfile + os.replace ensures readers never see a
         partially-written file.

    Returns MergeReport (success/failed; merged_files; errors).
    """
    tree = get_tree_by_node(node_id)
    if tree is None:
        return MergeReport(
            success=False, workspace_root=workspace_root,
            errors=["node_not_found"],
        )
    try:
        node = tree.get_node(node_id)
    except KeyError:
        return MergeReport(
            success=False, workspace_root=workspace_root,
            errors=["node_not_found"],
        )

    ws_resolved: Path = Path(workspace_root).resolve()
    if not ws_resolved.is_dir():
        return MergeReport(
            success=False, workspace_root=str(ws_resolved),
            errors=["workspace_not_a_directory"],
        )

    # ---- preflight: build full (path, content) list before touching disk ----
    pending: List[Tuple[Path, str]] = []
    preflight_errors: List[str] = []
    for rel_path, blob_hash in node.vfs_view.items():
        try:
            full: Path = (ws_resolved / rel_path).resolve()
        except OSError as exc:
            preflight_errors.append(f"path_resolve_failed:{rel_path}:{exc}")
            continue
        try:
            full.relative_to(ws_resolved)
        except ValueError:
            preflight_errors.append(f"path_escape:{rel_path}")
            continue
        content: Optional[str] = blob_storage.get(blob_hash)
        if content is None:
            preflight_errors.append(f"cas_miss:{rel_path}:{blob_hash[:8]}")
            continue
        pending.append((full, content))

    if preflight_errors:
        return MergeReport(
            success=False, workspace_root=str(ws_resolved),
            errors=preflight_errors,
        )

    # ---- atomic per-file writes ----
    write_errors: List[str] = []
    written: int = 0
    for full, content in pending:
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=str(full.parent),
                prefix=".__ailienant_tmp_",
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            os.replace(tmp_path, str(full))
            written += 1
        except OSError as exc:
            write_errors.append(f"write_failed:{full}:{exc}")
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---- prune + audit ----
    pruned: int = tree.prune_branch(node_id)
    mcts_checkpointer.record_prune(node_id, "user_merge_applied")

    return MergeReport(
        success=not write_errors,
        merged_files=written,
        workspace_root=str(ws_resolved),
        errors=write_errors,
        prune_count=pruned,
    )
