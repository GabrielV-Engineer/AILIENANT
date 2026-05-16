# brain/mcts/registry.py
"""Phase 3.4.5 — Process-local node_id -> MCTSTree registry.

API endpoints look up a live tree by any of its node_ids without plumbing a
tree instance through the request path. Trees self-register on construction +
expansion; they self-unregister on prune.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from brain.mcts.tree import MCTSTree

_registry: Dict[str, "MCTSTree"] = {}
_lock: threading.Lock = threading.Lock()


def register_node(node_id: str, tree: "MCTSTree") -> None:
    """Map node_id -> tree. Overwrites silently if node_id already present."""
    with _lock:
        _registry[node_id] = tree


def unregister_node(node_id: str) -> None:
    """Drop node_id from the registry. No-op if absent."""
    with _lock:
        _registry.pop(node_id, None)


def get_tree_by_node(node_id: str) -> Optional["MCTSTree"]:
    """Return the live MCTSTree owning node_id, or None."""
    with _lock:
        return _registry.get(node_id)


def clear_registry() -> None:
    """Test-only helper: drop all entries."""
    with _lock:
        _registry.clear()
