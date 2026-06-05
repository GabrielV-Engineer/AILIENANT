"""
core/indexer.py — Lazy background workspace indexer.

Triggered once per session via client_workspace_init. Batches file indexing
(_BATCH_SIZE=8, 0.1s throttle between batches) through the existing compute_pool.
Persists progress to indexed_files for crash-resume. Broadcasts INDEXING_PROGRESS
over WebSocket after each batch. Sets worker process priority to BELOW_NORMAL/nice=10.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

import psutil

from core.compute_pool import compute_pool
from core.db import (
    get_indexed_count,
    get_indexed_hash,
    purge_file_nodes,
    upsert_dependencies,
    upsert_indexed_file,
)
from brain.memory import index_file_sync
from shared.contracts import IndexingRequest, IndexingResult, detect_language

logger = logging.getLogger("LAZY_INDEXER")

_BATCH_SIZE: int = 8
_BATCH_SLEEP_S: float = 0.1
_INDEX_THRESHOLD: float = 0.05  # skip full crawl if >= (1 - 0.05) * total already indexed
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"})

# Reactive-index circuit breaker tuning. A file that fails this many times in a
# row trips its key OPEN for the cooldown; the first attempt afterwards is a
# half-open trial. Kept deliberately small — a downed embedding backend should be
# noticed in a handful of saves, not after hundreds of doomed retries.
_FAIL_THRESHOLD: int = 5
_COOLDOWN_S: float = 30.0


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


def _ollama_model_present(names: set[str], want: str) -> bool:
    """Return True if `want` matches any installed Ollama model name.

    Ollama's /api/tags reports tagged names (e.g. "nomic-embed-text:latest"), so a
    naive equality check falsely reports a freshly-pulled model as missing. Match
    on the tag-stripped, lowercased base name with either-direction prefix so
    "nomic-embed-text" ≡ "nomic-embed-text:latest" and registry-qualified names
    still resolve.
    """
    def _base(n: str) -> str:
        return n.split(":", 1)[0].strip().lower()

    want_base = _base(want)
    if not want_base:
        return False
    return any(
        _base(n) == want_base
        or _base(n).startswith(want_base)
        or want_base.startswith(_base(n))
        for n in names
        if n
    )


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
        pool = getattr(compute_pool, "_pool", None)
        processes = getattr(pool, "_processes", {})
        return list(processes.keys())
    except Exception:
        logger.debug("Could not resolve worker PIDs for priority adjustment — skipping.")
        return []


class SingleFlightCoordinator:
    """Per-key single-flight with trailing re-run.

    Under the Push model a single file can be re-indexed by overlapping requests:
    a slow ``compute_pool`` run for one file may still be in flight when the next
    debounce window dispatches the same file, wasting work and risking a stale
    write landing after a fresh one. This coordinator guarantees at most one
    in-flight coroutine per key. A request arriving while its key is running does
    NOT overlap; it records the latest coroutine factory as ``pending`` so exactly
    one more run fires after the current finishes (trailing edge → the freshest
    content always wins, never a lost update). Distinct keys run concurrently.
    """

    def __init__(self) -> None:
        self._running: Set[str] = set()
        self._pending: Dict[str, Callable[[], Awaitable[None]]] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        """Run ``factory()`` for ``key`` unless one is in flight; then coalesce.

        ``factory`` is a zero-arg callable that builds the coroutine fresh on
        each invocation (so a trailing re-run captures the latest content the
        caller closed over), not an already-awaited coroutine.
        """
        if key in self._running:
            self._pending[key] = factory  # coalesce — newest factory wins
            return
        self._running.add(key)
        try:
            await factory()
            # Trailing edge: drain re-runs queued while this key was in flight.
            # Each await may enqueue a newer pending entry; loop until quiescent.
            while key in self._pending:
                await self._pending.pop(key)()
        finally:
            self._running.discard(key)


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
        # Stored on each start() call so retry() can re-enter without a new WS event.
        self._last_workspace_root: str | None = None
        self._last_project_id: str | None = None
        self._last_session_id: str | None = None

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    @property
    def progress_percentage(self) -> float:
        if self._total == 0:
            return 0.0
        return round(self._current / self._total * 100.0, 1)

    async def _preflight_check(self) -> str | None:
        """
        Verify the embedding backend is reachable before touching any files.

        Provider-agnostic (Phase 7.9.B.12): resolves the active embedding target
        from the BYOM preset and validates it per provider — local engines are
        probed; cloud providers are gated on key presence (never a local-port
        ping). Returns None if OK, else a human-readable, actionable reason.
        Resets _is_running so a later retry (after the user fixes config) re-enters.
        """
        import httpx
        from core.config.embedding_resolver import get_embedding_target

        t = get_embedding_target()

        # --- Non-local targets: cloud key validation or legacy proxy health -----
        if not t.is_local:
            if t.provider == "proxy":
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{t.api_base}/health", timeout=3.0)
                        if resp.status_code >= 500:
                            return (
                                f"LiteLLM proxy at {t.api_base} returned HTTP "
                                f"{resp.status_code}. Check your proxy configuration."
                            )
                except Exception:
                    return (
                        f"LiteLLM proxy not reachable at {t.api_base}. Apply a BYOM "
                        "preset, start the proxy, or set AILIENANT_MODEL_EMBEDDING."
                    )
                return None
            if t.model == "(none)" or t.provider == "anthropic":
                return (
                    "Anthropic has no embeddings API. Set OPENAI_API_KEY or enable a "
                    "local engine (Ollama/LM Studio) for workspace indexing."
                )
            if not t.api_key:
                return (
                    f"API key required for {t.provider} embeddings. Add it to the BYOM "
                    "endpoint configuration."
                )
            return None

        # --- Local engines: probe the endpoint + verify the embed model ---------
        if not t.api_base:
            return "No embedding endpoint configured. Apply a BYOM preset to set one."
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                if t.provider == "ollama":
                    resp = await client.get(f"{t.api_base}/api/tags")
                    resp.raise_for_status()
                    names = {m.get("name", "") for m in resp.json().get("models", [])}
                    want = t.model.split("/", 1)[1] if "/" in t.model else t.model
                    if want and not _ollama_model_present(names, want):
                        return (
                            f"Embedding model '{want}' not installed in Ollama. "
                            f"Run: ollama pull {want}"
                        )
                else:
                    # LM Studio / vLLM / custom — OpenAI-compatible /v1/models
                    resp = await client.get(f"{t.api_base}/models")
                    resp.raise_for_status()
        except Exception:
            return (
                f"{t.provider} engine not reachable at {t.api_base}. Start it or "
                "update the BYOM endpoint, then it will index automatically."
            )
        return None

    async def start(self, workspace_root: str, project_id: str, session_id: str) -> None:
        """Trigger background indexing. No-op if already running or completed."""
        if self._is_running or self._is_complete:
            return
        self._last_workspace_root = workspace_root
        self._last_project_id = project_id
        self._last_session_id = session_id
        self._is_running = True
        asyncio.create_task(
            self._run(workspace_root, project_id, session_id),
            name=f"lazy_index:{project_id}",
        )

    async def retry(self) -> bool:
        """Re-attempt indexing after a preflight failure.

        No-op if already running, complete, or no workspace is known yet.
        Returns True if a retry task was enqueued.
        """
        if self._is_running or self._is_complete or not self._last_workspace_root:
            return False
        await self.start(
            self._last_workspace_root,
            self._last_project_id or "",
            self._last_session_id or "",
        )
        return True

    async def _run(self, workspace_root: str, project_id: str, session_id: str) -> None:
        # Deferred import to break indexer → websocket_manager circular load at module level
        from api.websocket_manager import vfs_manager

        try:
            # Pre-flight: abort immediately if embedding backend is unreachable.
            reason = await self._preflight_check()
            if reason:
                logger.warning("LazyIndexer: pre-flight failed — %s", reason)
                self._is_running = False  # allow retry after config is fixed
                await vfs_manager.broadcast_indexing_error(session_id, reason)
                return

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
                        from core.vfs_middleware import VFSMiddleware as _VFS  # deferred — avoid circular at module level
                        _vfs_result = _VFS().read_safe(
                            file_path,
                            project_id=project_id,
                            project_root=workspace_root,
                            session_id=session_id,
                        )
                        if not _vfs_result.ok or _vfs_result.content is None:
                            logger.debug(
                                "LazyIndexer: VFS skip %s: %s", file_path, _vfs_result.error
                            )
                            continue
                        content = _vfs_result.content
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


class _ReactiveBreaker:
    """Per-(project, file) failure-streak gate for reactive indexing.

    A single malformed file that always fails to parse, or a transient embedding
    outage, must not turn every save into a doomed retry. After _FAIL_THRESHOLD
    consecutive failures on a key the breaker OPENs for _COOLDOWN_S; the first
    attempt after the cooldown is a half-open trial. A success — or an explicit
    clear when the file is deleted — removes the key entirely, so healthy and
    deleted files retain no state and the map tracks only currently-failing files
    (bounded by the number of distinct failing paths, never by history).
    """

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._state: Dict[str, Tuple[int, float]] = {}  # key -> (failures, opened_at)
        self._time = time_fn

    def allow(self, key: str) -> bool:
        entry = self._state.get(key)
        if entry is None:
            return True
        failures, opened_at = entry
        if failures < _FAIL_THRESHOLD:
            return True  # still CLOSED
        # OPEN: shed until the cooldown elapses, then permit one half-open trial.
        return (self._time() - opened_at) >= _COOLDOWN_S

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)

    def record_failure(self, key: str) -> None:
        failures = self._state.get(key, (0, 0.0))[0] + 1
        # Stamp opened_at whenever the threshold is met so a failed half-open trial
        # restarts the cooldown window instead of immediately re-permitting.
        opened_at = self._time() if failures >= _FAIL_THRESHOLD else 0.0
        self._state[key] = (failures, opened_at)

    def clear(self, key: str) -> None:
        self._state.pop(key, None)


class ReactiveIndexer:
    """Single idempotent entry for incremental (per-save) indexing.

    Both real writers under the Push model — human saves and the agent's
    applyEdit, which echoes back through the editor's save event — converge here.
    A content-hash gate makes a byte-identical re-save a cheap no-op (no duplicate
    AST extraction, no duplicate embedding cost). On a real change it updates the
    dependency graph AND the vector store in one pass, under the real project_id
    so the agent's RAG consumer can actually see the edit. A per-key circuit
    breaker keeps a poison-pill file or a downed backend from being hammered.
    """

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._breaker = _ReactiveBreaker(time_fn)

    async def index(
        self,
        filepath: str,
        content: str,
        project_id: str,
        workspace_root: str,
        on_deps_changed: Callable[[], None],
    ) -> None:
        """Index one file if its content changed; otherwise skip (idempotent).

        ``content`` may be empty (a telemetry save carries no body) — the freshest
        bytes are then read from the RAM-VFS buffer or disk. ``on_deps_changed`` is
        invoked only when dependency edges were written, so the caller can debounce
        the graph-analytics pass without this module importing the scheduler.
        """
        key = f"{project_id}\x00{filepath}"
        if not self._breaker.allow(key):
            logger.debug("ReactiveIndexer: breaker OPEN — shedding %s", filepath)
            return

        lang = detect_language(filepath)
        if not lang:
            return  # unsupported file type — no-op

        resolved = content or self._read_vfs(filepath, project_id, workspace_root)
        if not resolved:
            return  # nothing to index (empty body and VFS read unavailable)

        digest = hashlib.sha256(resolved.encode("utf-8", errors="replace")).hexdigest()
        if digest == await get_indexed_hash(filepath, project_id):
            logger.debug("ReactiveIndexer: unchanged %s — skip (idempotent).", filepath)
            return

        # Confirmed change → evict any LLM responses cached against the old bytes.
        # The same save event that refreshes RAG deterministically drops the stale
        # plan/edit, so a hit can never serve a spec written for outdated content.
        from core.response_cache import response_cache  # deferred — avoid import cycle
        response_cache.invalidate_path(filepath)

        try:
            req = IndexingRequest(file_path=filepath, content=resolved, language_id=lang)
            result: IndexingResult = await compute_pool.run(index_file_sync, req)
            if not result.success:
                logger.warning("ReactiveIndexer: index failed for %s: %s", filepath, result.error)
                self._breaker.record_failure(key)
                return
            if result.imports:
                await upsert_dependencies(result.file_path, result.imports, project_id)
                on_deps_changed()
            await upsert_indexed_file(filepath, project_id, content_hash=digest)
            embedded = await self._semantic_upsert(filepath, resolved, project_id)
            if embedded:
                self._breaker.record_success(key)
            else:
                self._breaker.record_failure(key)
        except Exception as exc:
            logger.error("ReactiveIndexer: dispatch error for %s: %s", filepath, exc)
            self._breaker.record_failure(key)

    async def purge(self, filepath: str, project_id: str, workspace_root: str) -> None:
        """Eradicate a deleted file from both the graph and the vector store.

        Migrates the graph (ghost-prune of edges, PPR, indexed_files) and evicts
        the LanceDB vector so a stale embedding cannot pollute RAG before the next
        manual janitor run. Clears any breaker state so a path that is recreated
        later starts from a clean slate.
        """
        await purge_file_nodes(filepath, project_id)
        await self._semantic_delete(filepath, project_id)
        self._breaker.clear(f"{project_id}\x00{filepath}")
        from core.response_cache import response_cache  # deferred — avoid import cycle
        response_cache.invalidate_path(filepath)
        logger.info("ReactiveIndexer: purged %s (graph + vector)", filepath)

    # ── Side-effect helpers (deferred imports isolate optional subsystems) ─────

    def _read_vfs(self, filepath: str, project_id: str, workspace_root: str) -> Optional[str]:
        """Return the freshest bytes from the RAM-VFS buffer or disk; None if absent."""
        from core.vfs_middleware import VFSMiddleware  # deferred — avoid circular import
        try:
            res = VFSMiddleware().read_safe(
                filepath, project_id=project_id, project_root=workspace_root
            )
        except Exception as exc:
            logger.debug("ReactiveIndexer: VFS read error for %s: %s", filepath, exc)
            return None
        if not res.ok or res.content is None:
            logger.debug("ReactiveIndexer: VFS skip %s: %s", filepath, res.error)
            return None
        return res.content

    async def _semantic_upsert(self, filepath: str, content: str, project_id: str) -> bool:
        from core.memory.semantic_memory import SemanticMemoryManager  # deferred import
        try:
            return await SemanticMemoryManager().semantic_upsert(
                file_path=filepath, content=content, workspace_hash=project_id
            )
        except Exception as exc:
            logger.debug("ReactiveIndexer: semantic upsert failed (non-fatal): %s", exc)
            return False

    async def _semantic_delete(self, filepath: str, project_id: str) -> None:
        from core.memory.semantic_memory import SemanticMemoryManager  # deferred import
        try:
            await SemanticMemoryManager().semantic_delete(filepath, workspace_hash=project_id)
        except Exception as exc:
            logger.debug("ReactiveIndexer: semantic delete failed (non-fatal): %s", exc)


# Global singleton — imported by main.py
reactive_indexer = ReactiveIndexer()
