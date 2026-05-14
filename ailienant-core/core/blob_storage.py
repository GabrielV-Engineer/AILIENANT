# ailienant-core/core/blob_storage.py
#
# Phase 2.2.D — Content-Addressable Storage for VFS blobs.
# VFSFile.content is replaced by VFSFile.blob_hash (blake2b hex).
# The actual file text lives here, keyed by its hash.
# This keeps the LangGraph checkpoint tiny (hashes only) regardless of file size.

import hashlib
import re
import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("BLOB_STORAGE")

_DEFAULT_MAX_ENTRIES: int = 4096

_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_diff(original: str, patch: str) -> str:
    """Apply a unified diff string to original content.

    Implements a minimal unified-diff applier that:
      - Skips file header lines (--- / +++ )
      - Parses @@ hunk headers (supports omitted count, i.e. @@ -L +L @@)
      - Validates context/removal lines against the original (raises ValueError on mismatch)
      - Rebuilds the output by replaying context, removals, and additions

    Raises ValueError on bad hunk so callers can fall back to a full-file write.
    """
    original_lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)

    result: list[str] = []
    orig_pos = 0  # 0-indexed cursor into original_lines

    i = 0
    while i < len(patch_lines):
        line = patch_lines[i]

        # File header lines — skip
        if line.startswith("--- ") or line.startswith("+++ "):
            i += 1
            continue

        m = _HUNK_HEADER.match(line)
        if not m:
            i += 1
            continue

        orig_start = int(m.group(1)) - 1  # convert to 0-indexed
        i += 1

        # Copy unchanged lines from original up to this hunk start
        if orig_start > orig_pos:
            result.extend(original_lines[orig_pos:orig_start])
        orig_pos = orig_start

        # Process hunk body
        while i < len(patch_lines):
            hunk_line = patch_lines[i]
            if _HUNK_HEADER.match(hunk_line) or hunk_line.startswith("--- ") or hunk_line.startswith("+++ "):
                break  # next hunk or file header

            if hunk_line.startswith("+"):
                result.append(hunk_line[1:])
                i += 1
            elif hunk_line.startswith("-"):
                expected = hunk_line[1:]
                if orig_pos >= len(original_lines):
                    raise ValueError(
                        f"Hunk removal at line {orig_pos + 1} past end of file"
                    )
                actual = original_lines[orig_pos]
                if actual.rstrip("\n") != expected.rstrip("\n"):
                    raise ValueError(
                        f"Hunk mismatch at line {orig_pos + 1}: "
                        f"expected {expected!r}, got {actual!r}"
                    )
                orig_pos += 1
                i += 1
            elif hunk_line.startswith(" "):
                if orig_pos < len(original_lines):
                    result.append(original_lines[orig_pos])
                orig_pos += 1
                i += 1
            elif hunk_line.startswith("\\ "):
                # "\ No newline at end of file" — ignore
                i += 1
            else:
                i += 1

    # Append any trailing original lines after the last hunk
    result.extend(original_lines[orig_pos:])
    return "".join(result)


class ContentAddressableStorage:
    """RAM-backed content-addressable store keyed by blake2b hex digest.

    All file content written by CoderAgent (Phase 4) is interned here.
    VFSFile in the LangGraph checkpoint only carries the hash — serialised
    size is O(hash_length) regardless of file size.

    Eviction: LRU policy with configurable max_entries cap. Oldest blobs are
    evicted when capacity is reached; a WARNING is logged each time so operators
    can tune max_entries before OOM pressure builds.

    Thread-safety: single-process, single-event-loop — no locking needed.
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._store: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries

    def put(self, content: str) -> str:
        """Hash content with blake2b, store it, return the hex digest.

        Deduplication: if the hash already exists, mark it as most-recently-used
        and return immediately (no write, no eviction).
        """
        h = hashlib.blake2b(content.encode("utf-8")).hexdigest()
        if h in self._store:
            self._store.move_to_end(h)
            return h
        if len(self._store) >= self._max_entries:
            evicted, _ = self._store.popitem(last=False)
            logger.warning(
                "LRU eviction: blob %s.. evicted (store at capacity=%d entries). "
                "If blobs are being evicted prematurely, increase max_entries.",
                evicted[:8], self._max_entries,
            )
        self._store[h] = content
        return h

    def get(self, blob_hash: str) -> Optional[str]:
        """Retrieve content by hash. Returns None if not found."""
        return self._store.get(blob_hash)

    def apply_patch(self, blob_hash: str, diff_patch: str) -> Optional[str]:
        """Apply a unified diff to stored content; return new hash or None on failure.

        None signals the caller to fall back to a full-file write
        (caller should request the full file content from CoderAgent).
        """
        original = self._store.get(blob_hash, "")
        try:
            patched = _apply_unified_diff(original, diff_patch)
            new_hash = self.put(patched)
            logger.debug(
                "apply_patch: %s..→%s.. (%d→%d chars)",
                blob_hash[:8], new_hash[:8], len(original), len(patched),
            )
            return new_hash
        except Exception as exc:
            logger.warning("apply_patch failed for %s..: %s", blob_hash[:8], exc)
            return None

    def __len__(self) -> int:
        return len(self._store)


# Module-level singleton — imported by agents and reducer nodes.
blob_storage = ContentAddressableStorage()
