# tools/validation/virtual_doc.py
"""Phase 3.4.4 — Flyweight virtual document provider.

Overlays an MCTS vfs_view (path -> blob_hash) over physical disk for read-only
access. Used by Micro-Isolate validators so candidate code is checked against
the parallel-universe state without ever mutating the filesystem.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from core.blob_storage import blob_storage

logger = logging.getLogger("VIRTUAL_DOC")


class VirtualDocumentProvider:
    """Read-only flyweight: path -> (CAS blob | disk content | None)."""

    def __init__(self, vfs_view: Dict[str, str]) -> None:
        self._vfs_view: Dict[str, str] = {
            os.path.normpath(p): h for p, h in vfs_view.items()
        }

    def read(self, path: str) -> Optional[str]:
        """Return content of `path` from CAS (if shadowed) or disk; None on miss."""
        norm: str = os.path.normpath(path)
        if norm in self._vfs_view:
            blob_hash: str = self._vfs_view[norm]
            content: Optional[str] = blob_storage.get(blob_hash)
            if content is None:
                logger.warning(
                    "VirtualDoc: CAS miss for %s -> %s.. (LRU evicted?)",
                    norm, blob_hash[:8],
                )
            return content
        try:
            with open(norm, "r", encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("VirtualDoc: disk read failed for %s: %s", norm, exc)
            return None

    def is_shadowed(self, path: str) -> bool:
        """True if `path` is overridden by the vfs_view (not falling back to disk)."""
        return os.path.normpath(path) in self._vfs_view
