"""
core/indexer.py — Lazy background workspace indexer.

Triggered once per session via client_workspace_init. Batches file indexing
(_BATCH_SIZE=8, 0.1s throttle between batches) through the existing compute_pool.
Persists progress to indexed_files for crash-resume. Broadcasts INDEXING_PROGRESS
over WebSocket after each batch. Sets worker process priority to BELOW_NORMAL/nice=10.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import List

import psutil

from core.compute_pool import compute_pool
from core.db import get_indexed_count, upsert_indexed_file, upsert_dependencies
from brain.memory import index_file_sync
from shared.contracts import IndexingRequest, IndexingResult, detect_language

logger = logging.getLogger("LAZY_INDEXER")

_BATCH_SIZE: int = 8
_BATCH_SLEEP_S: float = 0.1
_INDEX_THRESHOLD: float = 0.05  # skip full crawl if >= (1 - 0.05) * total already indexed
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"})


def _collect_eligible_files(workspace_root: str) -> List[str]:
    """Walk workspace_root and return all files with a supported language extension.

    Prunes hidden dirs, __pycache__, node_modules, venv, etc. to avoid indexing
    generated or vendor artifacts.
    """
    eligible: List[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if detect_language(full):
                eligible.append(full)
    return eligible


def _set_low_priority(pid: int) -> None:
    """Set a process to low OS scheduling priority (best-effort, non-fatal)."""
    try:
        proc = psutil.Process(pid)
        if sys.platform == "win32":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(10)
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        pass


def _get_pool_pids() -> List[int]:
    """Extract live worker PIDs from the compute pool.

    Accesses ProcessPoolExecutor._processes, a CPython internal detail.
    Uses chained getattr() calls so any version change or AttributeError is
    non-fatal — the indexer MUST NOT crash if it cannot set process priority.
    """
    try:
        pool = getattr(compute_pool, "_pool", None)  # type: ignore[attr-defined]
        processes = getattr(pool, "_processes", {})   # type: ignore[union-attr]
        return list(processes.keys())
    except Exception:
        logger.debug("Could not resolve worker PIDs for priority adjustment — skipping.")
        return []


class LazyIndexer:
    """
    Async background workspace indexer. Use global `lazy_indexer` singleton.

    start() is idempotent: no-op if already running or completed.
    Crash-resume: files already in indexed_files table are counted; if enough are
    indexed the threshold check prevents re-crawling on restart.
    """

    def __init__(self) -> None:
        self._is_complete: bool = False
        self._is_running: bool = False
        self._current: int = 0
        self._total: int = 0

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    @property
    def progress_percentage(self) -> float:
        if self._total == 0:
            return 0.0
        return round(self._current / self._total * 100.0, 1)

    async def start(self, workspace_root: str, project_id: str, session_id: str) -> None:
        """Trigger background indexing. No-op if already running or completed."""
        if self._is_running or self._is_complete:
            return
        self._is_running = True
        asyncio.create_task(
            self._run(workspace_root, project_id, session_id),
            name=f"lazy_index:{project_id}",
        )

    async def _run(self, workspace_root: str, project_id: str, session_id: str) -> None:
        # Deferred import to break indexer → websocket_manager circular load at module level
        from api.websocket_manager import vfs_manager

        try:
            eligible = _collect_eligible_files(workspace_root)
            self._total = len(eligible)

            if self._total == 0:
                logger.info("LazyIndexer: no eligible files in %s", workspace_root)
                self._is_complete = True
                return

            # Skip full crawl if enough files are already indexed (crash-resume)
            already = await get_indexed_count(project_id)
            if already >= self._total * (1.0 - _INDEX_THRESHOLD):
                logger.info(
                    "LazyIndexer: %d/%d files already indexed — skipping full crawl.",
                    already, self._total,
                )
                self._current = self._total
                self._is_complete = True
                await vfs_manager.broadcast_indexing_complete(session_id)
                return

            logger.info("LazyIndexer: indexing %d files (project=%s)", self._total, project_id)

            # Apply low OS scheduling priority to worker processes
            for pid in _get_pool_pids():
                _set_low_priority(pid)

            for batch_start in range(0, self._total, _BATCH_SIZE):
                batch = eligible[batch_start: batch_start + _BATCH_SIZE]
                for file_path in batch:
                    try:
                        lang = detect_language(file_path)
                        if not lang:
                            continue
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                                content = fh.read()
                        except OSError as exc:
                            logger.debug("LazyIndexer: cannot read %s: %s", file_path, exc)
                            continue
                        req = IndexingRequest(file_path=file_path, content=content, language_id=lang)
                        result: IndexingResult = await compute_pool.run(index_file_sync, req)
                        if result.success:
                            await upsert_indexed_file(file_path, project_id)
                            if result.imports:
                                await upsert_dependencies(file_path, result.imports, project_id)
                            # ── Phase 3.1: Semantic upsert (fire-and-forget) ──────────
                            # Deferred import: isolates even import errors from indexing.
                            try:
                                from core.memory.semantic_memory import SemanticMemoryManager
                                await SemanticMemoryManager().semantic_upsert(
                                    file_path=file_path,
                                    content=content,
                                    workspace_hash=project_id,
                                )
                            except Exception as _sem_err:
                                logger.debug("Semantic upsert failed (non-fatal): %s", _sem_err)
                            # ─────────────────────────────────────────────────────────
                    except Exception as exc:
                        logger.warning("LazyIndexer: skip %s: %s", file_path, exc)
                    finally:
                        self._current += 1

                await vfs_manager.broadcast_indexing_progress(session_id, self._current, self._total)
                await asyncio.sleep(_BATCH_SLEEP_S)

            self._is_complete = True
            await vfs_manager.broadcast_indexing_complete(session_id)
            logger.info("LazyIndexer: done — %d files indexed for project=%s", self._total, project_id)

        except Exception as exc:
            logger.error("LazyIndexer: fatal error: %s", exc, exc_info=True)
        finally:
            self._is_running = False


# Global singleton — imported by main.py
lazy_indexer = LazyIndexer()
