# tests/test_mcts_mirror.py
"""Phase 3.4.5 DoD — MCTS Mirror service: registry, get_virtual_file, apply_merge.

Covers:
  * Registry self-management on construction / expand / prune
  * get_virtual_file reads via CAS through VirtualDocumentProvider
  * apply_merge sandboxing (path escape rejection)
  * apply_merge preflight (CAS miss aborts before any disk write)
  * apply_merge atomic write + prune + checkpoint audit
  * HTTP smoke tests via FastAPI TestClient
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from api.mcts_mirror import apply_merge, get_virtual_file
from brain.mcts.registry import clear_registry, get_tree_by_node
from brain.mcts.tree import MCTSTree
from brain.state import MissionSpecification, WBSStep
from core.blob_storage import blob_storage


@pytest.fixture(autouse=True)
def _clear_registry():
    clear_registry()
    yield
    clear_registry()


def _make_mission(outcome: str = "test") -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome,
        scope=["x"],
        constraints=["y"],
        decisions=["z"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="Refactor",
                action="write_file",
                target_file="foo.py",
                description="d",
            )
        ],
        checks=["c"],
    )


# ---------- registry ----------

def test_registry_populated_on_construction_and_expand() -> None:
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    assert get_tree_by_node(tree.root_id) is tree

    child = tree.expand(tree.root_id, "a", {}, _make_mission("c"))
    assert get_tree_by_node(child.node_id) is tree


def test_registry_clears_on_prune() -> None:
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(tree.root_id, "a", {}, _make_mission("c"))
    assert get_tree_by_node(child.node_id) is tree
    tree.prune_branch(child.node_id)
    assert get_tree_by_node(child.node_id) is None
    # Root still present because we only pruned the child.
    assert get_tree_by_node(tree.root_id) is tree


# ---------- get_virtual_file ----------

def test_get_virtual_file_reads_cas() -> None:
    blob_hash = blob_storage.put("# dreamed code\n")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a", {"src/dream.py": blob_hash}, _make_mission("c"),
    )
    assert get_virtual_file(child.node_id, "src/dream.py") == "# dreamed code\n"


def test_get_virtual_file_unknown_node_returns_none() -> None:
    assert get_virtual_file("nonexistent_node_id", "x.py") is None


# ---------- apply_merge: safety ----------

def test_apply_merge_rejects_path_escape(tmp_path: Path) -> None:
    blob_hash = blob_storage.put("evil")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a", {"../escape.txt": blob_hash}, _make_mission("c"),
    )
    report = apply_merge(child.node_id, str(tmp_path))
    assert report.success is False
    assert any("path_escape" in e for e in report.errors)
    # No files created in workspace or outside it.
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path.parent / "escape.txt").exists()


def test_apply_merge_aborts_on_cas_miss(tmp_path: Path) -> None:
    """CAS miss in preflight must abort BEFORE any disk write."""
    real_hash = blob_storage.put("real")
    fake_hash = "deadbeefcafebabe" + "0" * 32
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a",
        {"a.py": real_hash, "b.py": fake_hash},
        _make_mission("c"),
    )
    report = apply_merge(child.node_id, str(tmp_path))
    assert report.success is False
    assert any("cas_miss" in e for e in report.errors)
    # Preflight failure -> NEITHER file written, even though a.py's hash is valid.
    assert not (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()


def test_apply_merge_node_not_found(tmp_path: Path) -> None:
    report = apply_merge("nonexistent_node_id", str(tmp_path))
    assert report.success is False
    assert "node_not_found" in report.errors


def test_apply_merge_workspace_not_a_directory(tmp_path: Path) -> None:
    blob_hash = blob_storage.put("x")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(tree.root_id, "a", {"a.py": blob_hash}, _make_mission("c"))
    not_a_dir = tmp_path / "i_am_a_file"
    not_a_dir.write_text("not a directory")
    report = apply_merge(child.node_id, str(not_a_dir))
    assert report.success is False
    assert any("workspace_not_a_directory" in e for e in report.errors)


# ---------- apply_merge: happy path ----------

def test_apply_merge_writes_files_and_prunes(tmp_path: Path) -> None:
    h1 = blob_storage.put("content of A\n")
    h2 = blob_storage.put("content of B\n")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a",
        {"sub/a.py": h1, "b.py": h2},
        _make_mission("c"),
    )

    with patch(
        "api.mcts_mirror.mcts_checkpointer.record_prune"
    ) as mock_prune:
        report = apply_merge(child.node_id, str(tmp_path))

    assert report.success is True
    assert report.merged_files == 2
    assert report.errors == []
    assert report.prune_count >= 1
    # Phase 3.4.7 — merged_paths must contain both relative paths actually written.
    assert sorted(report.merged_paths) == ["b.py", "sub/a.py"]

    # Files written under tmp_path with exact content.
    assert (tmp_path / "sub" / "a.py").read_text(encoding="utf-8") == "content of A\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "content of B\n"

    # Tree was pruned + audit recorded.
    mock_prune.assert_called_once_with(child.node_id, "user_merge_applied")
    # Child registry entry gone after prune.
    assert get_tree_by_node(child.node_id) is None


# ---------- HTTP smoke tests ----------

@pytest.fixture
def http_client():
    """FastAPI TestClient lazily imported to avoid main.py side effects at collection."""
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        yield client


def test_http_get_virtual_file_404(http_client) -> None:
    resp = http_client.get("/api/v1/mcts/unknown_node/vfs", params={"path": "x.py"})
    assert resp.status_code == 404


def test_http_get_virtual_file_200(http_client) -> None:
    blob_hash = blob_storage.put("the dream\n")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a", {"d.py": blob_hash}, _make_mission("c"),
    )
    resp = http_client.get(
        f"/api/v1/mcts/{child.node_id}/vfs",
        params={"path": "d.py"},
    )
    assert resp.status_code == 200
    assert resp.text == "the dream\n"


def test_http_apply_merge(http_client, tmp_path: Path) -> None:
    blob_hash = blob_storage.put("merged via http\n")
    tree = MCTSTree(root_state=_make_mission("r"), root_vfs_view={})
    child = tree.expand(
        tree.root_id, "a", {"http.py": blob_hash}, _make_mission("c"),
    )
    with patch("api.mcts_mirror.mcts_checkpointer.record_prune"):
        resp = http_client.post(
            f"/api/v1/mcts/{child.node_id}/merge",
            json={"workspace_root": str(tmp_path)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["merged_files"] == 1
    assert (tmp_path / "http.py").read_text(encoding="utf-8") == "merged via http\n"
