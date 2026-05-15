# tests/test_mcts_daemon.py
"""Phase 3.4.3a DoD — MCTS tree, GC, checkpointer, daemon lifecycle."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from brain.daemon import OvernightDaemon
from brain.episodic.checkpointing import MCTSCheckpointer
from brain.mcts.tree import MCTSTree
from brain.state import MissionSpecification, WBSStep


# ---------- fixtures ----------

def _make_mission(outcome: str = "test-outcome") -> MissionSpecification:
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


@pytest.fixture
def tree() -> MCTSTree:
    return MCTSTree(
        root_state=_make_mission("root"),
        root_vfs_view={"foo.py": "hash-root"},
    )


@pytest.fixture
def mcts_db(tmp_path):
    cp = MCTSCheckpointer()
    cp.initialize(db_path=str(tmp_path / "mcts.sqlite"))
    yield cp
    cp.close()


# ---------- DoD: prune_branch clears vfs_view ----------

def test_prune_branch_clears_vfs_view(tree: MCTSTree) -> None:
    """prune_branch must clear vfs_view, mark descendants pruned, return count."""
    root = tree.root_id
    c1 = tree.expand(root, "a1", {"foo.py": "h1"}, _make_mission("c1")).node_id
    c2 = tree.expand(c1, "a2", {"foo.py": "h2"}, _make_mission("c2")).node_id
    assert tree.get_node(c1).vfs_view == {"foo.py": "h1"}
    assert tree.get_node(c2).vfs_view == {"foo.py": "h2"}

    pruned_count = tree.prune_branch(c1)

    assert pruned_count == 2
    assert tree.get_node(c1).is_pruned is True
    assert tree.get_node(c2).is_pruned is True
    assert tree.get_node(c1).vfs_view == {}
    assert tree.get_node(c2).vfs_view == {}
    # Root untouched.
    assert tree.get_node(root).is_pruned is False
    assert tree.get_node(root).vfs_view == {"foo.py": "hash-root"}


# ---------- gc_pruned ----------

def test_gc_pruned_removes_nodes_from_dict(tree: MCTSTree) -> None:
    root = tree.root_id
    c1 = tree.expand(root, "a1", {}, _make_mission("c1")).node_id
    c2 = tree.expand(c1, "a2", {}, _make_mission("c2")).node_id
    tree.prune_branch(c1)
    deleted = tree.gc_pruned()
    assert deleted == 2
    with pytest.raises(KeyError):
        tree.get_node(c1)
    with pytest.raises(KeyError):
        tree.get_node(c2)
    # Root remains.
    tree.get_node(root)


# ---------- record_stable promotes + inserts audit row ----------

def test_mark_stable_records_to_checkpoint(
    tree: MCTSTree, mcts_db: MCTSCheckpointer
) -> None:
    root = tree.root_id
    child = tree.expand(root, "build", {"foo.py": "hX"}, _make_mission("child"))
    child.reward = 0.87
    tree.mark_stable(child.node_id)

    with patch(
        "brain.episodic.checkpointing.checkpoint_manager.promote"
    ) as mock_promote:
        mcts_db.record_stable(child, thread_id="thread-1")
        mock_promote.assert_called_once_with("thread-1")

    conn = mcts_db._conn
    assert conn is not None
    row = conn.execute(
        "SELECT node_id, reward_R, mission_outcome FROM mcts_episodes "
        "WHERE node_id=?",
        (child.node_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == child.node_id
    assert abs(row[1] - 0.87) < 1e-9
    assert row[2] == "child"


# ---------- daemon start/stop lifecycle ----------

@pytest.mark.anyio
async def test_daemon_start_stop_no_op(
    tree: MCTSTree, mcts_db: MCTSCheckpointer
) -> None:
    """Daemon start/stop must complete cleanly without exceptions."""
    daemon = OvernightDaemon(tree=tree, checkpointer=mcts_db)
    daemon.start()
    await asyncio.sleep(0.05)
    assert daemon._task is not None
    assert not daemon._task.done()
    await daemon.stop()
    assert daemon._task.done()


# ---------- UCB1 selection ----------

def test_ucb_selection_picks_high_value_child(tree: MCTSTree) -> None:
    root = tree.root_id
    low = tree.expand(root, "lo", {}, _make_mission("lo"))
    high = tree.expand(root, "hi", {}, _make_mission("hi"))
    tree.get_node(root).visits = 10
    low.visits = 5
    low.total_value = 1.0   # avg 0.2
    high.visits = 5
    high.total_value = 4.0  # avg 0.8
    picked = tree.select_best_child(root)
    assert picked == high.node_id
