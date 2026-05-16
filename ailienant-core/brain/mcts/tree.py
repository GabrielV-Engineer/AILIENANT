# brain/mcts/tree.py
"""
Phase 3.4.3a — MCTS data structures.

Each MCTSNode represents a "parallel universe" with its own VFS view
(path -> blob_hash references into the existing ContentAddressableStorage).
Per-node CAS view keeps memory cost O(paths) per node instead of O(content);
prune_branch() drops the dict so CAS LRU can reclaim unreferenced blobs.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from brain.mcts.registry import register_node, unregister_node
from brain.state import MissionSpecification

logger = logging.getLogger("MCTS_TREE")


@dataclass
class MCTSNode:
    """One node in the MCTS tree. Holds a VFS view (CAS references) + UCB stats."""

    node_id: str
    parent_id: Optional[str]
    vfs_view: Dict[str, str]
    mission_state: MissionSpecification
    action: Optional[str] = None
    reward: float = -1.0  # -1.0 means "not yet evaluated by Nightmare Protocol"
    visits: int = 0
    total_value: float = 0.0
    children: List[str] = field(default_factory=list)
    is_pruned: bool = False
    is_stable: bool = False
    # Phase 3.4.8 — consecutive local-fixer failures. Triggers Circuit Breaker at 3.
    error_streak: int = 0


class MCTSTree:
    """In-memory MCTS tree. All operations are O(branching) or O(subtree)."""

    def __init__(
        self,
        root_state: MissionSpecification,
        root_vfs_view: Dict[str, str],
    ) -> None:
        root_id: str = uuid.uuid4().hex
        root: MCTSNode = MCTSNode(
            node_id=root_id,
            parent_id=None,
            vfs_view=dict(root_vfs_view),
            mission_state=root_state,
            action=None,
        )
        self._nodes: Dict[str, MCTSNode] = {root_id: root}
        self.root_id: str = root_id
        register_node(root_id, self)

    def __len__(self) -> int:
        return len(self._nodes)

    def get_node(self, node_id: str) -> MCTSNode:
        """Return node by id; raises KeyError if absent (e.g., gc_pruned removed it)."""
        return self._nodes[node_id]

    def expand(
        self,
        parent_id: str,
        action: str,
        new_vfs_view: Dict[str, str],
        child_mission_state: MissionSpecification,
    ) -> MCTSNode:
        """Append a child node under parent_id and return it."""
        parent: MCTSNode = self._nodes[parent_id]
        child_id: str = uuid.uuid4().hex
        child: MCTSNode = MCTSNode(
            node_id=child_id,
            parent_id=parent_id,
            vfs_view=dict(new_vfs_view),
            mission_state=child_mission_state,
            action=action,
        )
        self._nodes[child_id] = child
        parent.children.append(child_id)
        register_node(child_id, self)
        return child

    def prune_branch(self, node_id: str) -> int:
        """Mark node + all descendants pruned; clear their vfs_view dicts.

        Critical for heap safety: dropping vfs_view releases the only references
        keeping CAS blobs alive, so the next CAS LRU eviction can reclaim them.
        Returns the total number of nodes that were pruned (including node_id).
        """
        if node_id not in self._nodes:
            return 0

        from core.telemetry import log_routing_decision

        pruned: int = 0
        stack: List[str] = [node_id]
        while stack:
            current_id: str = stack.pop()
            node: Optional[MCTSNode] = self._nodes.get(current_id)
            if node is None or node.is_pruned:
                continue
            node.is_pruned = True
            node.vfs_view = {}
            unregister_node(current_id)
            pruned += 1
            stack.extend(node.children)

        log_routing_decision(
            session_id=node_id,
            source="mcts",
            target="prune",
            reason=f"prune_branch from node={node_id[:8]} count={pruned}",
        )
        return pruned

    def mark_stable(self, node_id: str) -> None:
        """Flag a node as worthy of episodic-checkpoint persistence."""
        self._nodes[node_id].is_stable = True

    def select_best_child(
        self,
        parent_id: str,
        c: float = 1.414,
    ) -> Optional[str]:
        """UCB1 selection. Returns the highest-score live child id, or None."""
        parent: MCTSNode = self._nodes[parent_id]
        if parent.visits <= 0:
            return None
        log_parent: float = math.log(parent.visits)
        best_id: Optional[str] = None
        best_score: float = -math.inf
        for child_id in parent.children:
            child: Optional[MCTSNode] = self._nodes.get(child_id)
            if child is None or child.is_pruned or child.visits <= 0:
                continue
            exploitation: float = child.total_value / child.visits
            exploration: float = c * math.sqrt(log_parent / child.visits)
            score: float = exploitation + exploration
            if score > best_score:
                best_score = score
                best_id = child_id
        return best_id

    def gc_pruned(self) -> int:
        """Delete pruned nodes from the internal dict. Returns count deleted.

        Parent children[] lists keep tombstone ids — cheap, and select_best_child
        already filters via self._nodes.get() so dead ids cause no harm.
        Root is never deleted, even if marked pruned.
        """
        to_delete: List[str] = [
            nid for nid, node in self._nodes.items()
            if node.is_pruned and nid != self.root_id
        ]
        for nid in to_delete:
            del self._nodes[nid]
        return len(to_delete)
