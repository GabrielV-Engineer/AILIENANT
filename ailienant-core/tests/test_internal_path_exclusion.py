"""AILIENANT's own runtime artifacts must never be surfaced to the agent.

The telemetry log (`.ailienant_telemetry.log`) is rewritten continuously while a
task runs, so any proposed move fails the host's optimistic-concurrency hash
check and strands the whole batch (the live failure that motivated DEBT-064).
These tests pin the three exclusion layers: the path predicate, the workspace
tree the planner sees, and the VFS read firewall.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from agents.workspace_context import _build_tree
from core.storage_paths import is_ailienant_internal_path
from core.vfs_middleware import VFSMiddleware


def test_predicate_matches_runtime_artifacts() -> None:
    assert is_ailienant_internal_path(".ailienant_telemetry.log")
    assert is_ailienant_internal_path(".ailienant_telemetry.log.1")
    assert is_ailienant_internal_path(r"C:\proj\.ailienant_telemetry.log")
    assert is_ailienant_internal_path("/home/u/proj/.ailienant_telemetry.log")
    assert is_ailienant_internal_path(".ailienant/AGENTS.md")
    assert is_ailienant_internal_path("/home/u/proj/.ailienant/plans/p.md")


def test_predicate_leaves_user_files_alone() -> None:
    assert not is_ailienant_internal_path("src/app.py")
    assert not is_ailienant_internal_path("telemetry.log")          # not AILIENANT's
    assert not is_ailienant_internal_path("ailienant_notes.md")     # not the home dir
    assert not is_ailienant_internal_path("")
    # AILIENANT.md is user-authored, shareable guidance — read/edited intentionally.
    assert not is_ailienant_internal_path(".ailienant/AILIENANT.md")
    assert not is_ailienant_internal_path("/home/u/proj/.ailienant/AILIENANT.md")


def test_workspace_tree_omits_the_telemetry_log(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".ailienant_telemetry.log").write_text("noise\n", encoding="utf-8")
    (tmp_path / ".ailienant_telemetry.log.1").write_text("older\n", encoding="utf-8")

    tree = "\n".join(_build_tree(tmp_path, max_depth=3, max_files=100))

    assert "app.py" in tree
    assert ".ailienant_telemetry.log" not in tree


def test_read_safe_ignores_the_telemetry_log(tmp_path: Path) -> None:
    log = tmp_path / ".ailienant_telemetry.log"
    log.write_text("secret telemetry\n", encoding="utf-8")
    # Unique project_id so the per-project ignore-spec cache never collides.
    project_id = uuid.uuid4().hex

    result = VFSMiddleware().read_safe(
        str(log), project_id=project_id, project_root=str(tmp_path),
    )

    assert not result.ok
    assert result.error == "FILE_IGNORED"
