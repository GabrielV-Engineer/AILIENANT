"""core/response_cache.py — bounded semantic response cache for LLM calls.

Extends the AST engine's blake2b content-hash primitive from caching parse
*trees* to caching deterministic LLM *responses*. Probe before a planner/coder
call; on an exact context match the prior result is served from memory (O(1))
instead of paying the network round-trip. Correctness rests on the key: every
context file's content-hash is folded in, so a one-byte edit yields a different
key (a miss). The reactive-index save hook additionally evicts entries for a
changed file, keeping the table tight without waiting for LRU pressure.

Concurrency contract:
  * The lock guards ONLY the synchronous dict mutations — it is never held across
    an ``await``. Call sites probe (lock released), run inference, then store
    (lock re-acquired); the network I/O sits strictly between the two.
  * ``_drop_locked`` is the single eviction chokepoint: it scrubs both the
    forward cache AND the reverse path index, so neither the LRU path nor active
    invalidation can leak keys into ``_paths`` (bounded-memory guarantee).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, OrderedDict as OrderedDictT, Set, Tuple

from core.ast_engine import ast_content_hash

# Caps sized for OOM safety: a few hundred entries of JSON-sized strings is
# kilobytes, not megabytes, and the TTL bounds staleness even if a save event is
# missed (e.g. an external edit the reactive indexer never observed).
_MAX_ENTRIES: int = 256
_TTL_S: float = 1800.0  # 30 minutes


class SemanticResponseCache:
    """Thread-safe bounded LRU response cache keyed by intent + context hash."""

    def __init__(
        self,
        max_entries: int = _MAX_ENTRIES,
        ttl_s: float = _TTL_S,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._time = time_fn
        # key -> (stored_at, value); insertion order is the LRU order.
        self._cache: "OrderedDictT[str, Tuple[float, str]]" = OrderedDict()
        # Reverse indexes for O(1) active eviction by file path.
        self._paths: Dict[str, Set[str]] = {}      # path -> keys referencing it
        self._key_paths: Dict[str, Set[str]] = {}  # key -> its paths (for cleanup)
        self._lock = threading.Lock()

    # -- key construction -----------------------------------------------------

    def build_key(
        self,
        *,
        intent: str,
        context: List[Tuple[str, str]],
        project_id: str,
        model: str,
    ) -> str:
        """Derive the cache key from intent + per-file content-hashes.

        Folds ``project_id`` and ``model`` in so cache entries never cross a
        project or a model boundary. Context pairs are sorted so key derivation
        is independent of retrieval order. Each file contributes its
        ``ast_content_hash`` (the same blake2b digest the AST tree cache uses),
        making a one-byte edit produce a different key.
        """
        h = hashlib.blake2b(digest_size=32)
        h.update(b"v1\x00")
        h.update(project_id.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(model.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(intent.encode("utf-8", "replace"))
        for path, content in sorted(context):
            h.update(b"\x00")
            h.update(path.encode("utf-8", "replace"))
            h.update(b"\x00")
            h.update(ast_content_hash(content).encode("ascii"))
        return h.hexdigest()

    # -- probe / store --------------------------------------------------------

    def probe(self, key: str) -> Optional[str]:
        """Return the cached value for ``key``, or ``None`` on miss/expiry.

        A hit refreshes LRU recency; an expired entry is dropped in passing.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if (self._time() - stored_at) > self._ttl_s:
                self._drop_locked(key)
                return None
            self._cache.move_to_end(key)
            return value

    def store(self, key: str, value: str, paths: List[str]) -> None:
        """Cache ``value`` under ``key`` and register the files it depends on.

        Evicts the least-recently-used entries (each through ``_drop_locked``, so
        the reverse index never leaks) until the size cap holds.
        """
        with self._lock:
            self._cache[key] = (self._time(), value)
            self._cache.move_to_end(key)
            key_paths = self._key_paths.setdefault(key, set())
            for path in paths:
                if not path:
                    continue
                key_paths.add(path)
                self._paths.setdefault(path, set()).add(key)
            while len(self._cache) > self._max_entries:
                oldest_key = next(iter(self._cache))
                self._drop_locked(oldest_key)

    # -- eviction -------------------------------------------------------------

    def invalidate_path(self, path: str) -> None:
        """Drop every cache entry that referenced ``path`` (best-effort).

        Called from the reactive-index save/purge hook so a changed or deleted
        file deterministically destroys any plan/edit cached against its old
        content — the same filesystem event that refreshes RAG clears the cache.
        """
        with self._lock:
            for key in list(self._paths.get(path, ())):
                self._drop_locked(key)

    def clear(self) -> None:
        """Reset all state (lifecycle / test reset)."""
        with self._lock:
            self._cache.clear()
            self._paths.clear()
            self._key_paths.clear()

    def _drop_locked(self, key: str) -> None:
        """Remove ``key`` from the cache AND scrub it from the reverse index.

        The single GC chokepoint shared by LRU eviction, TTL expiry, and active
        invalidation: for each path the key referenced, discard the key and pop
        the path entry once its set empties, so ``_paths`` stays bounded by the
        set of *currently cached* files — never by history. Caller holds the lock.
        """
        self._cache.pop(key, None)
        for path in self._key_paths.pop(key, ()):
            referrers = self._paths.get(path)
            if referrers is None:
                continue
            referrers.discard(key)
            if not referrers:
                self._paths.pop(path, None)


# Global singleton — imported by the planner, coder, and reactive indexer.
response_cache = SemanticResponseCache()
