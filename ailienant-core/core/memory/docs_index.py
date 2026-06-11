# core/memory/docs_index.py
"""Product-documentation retriever for the analyst tutor.

A dedicated, workspace-independent vector index over the shipped AILIENANT
"what / how" docs (HowItWorks, HowToUseIt, and the public README) so the analyst
can answer how-to / help questions about the product itself. It is isolated from
the per-workspace code index (its own LanceDB table), so the Memory Janitor's
per-workspace GC never touches it.

Concurrency & resilience contract:
  * **Idempotent ingestion** — the build is guarded by a process-level
    ``asyncio.Lock`` *and* a cross-process ``FileLock`` (whose blocking acquire
    is delegated to a worker thread so it never freezes the event loop), with a
    double-check inside the lock. Concurrent first-uses / UI refreshes collapse
    to a single build.
  * **Versioned rebuild** — the index records ``(docs_content_hash,
    embedding_model_id)``. When the active embedding model changes (e.g. a BYOM
    preset switch) the stored vectors no longer match; a rebuild is dispatched
    in the background, never synchronously in the request path.
  * **Cooperative cancellation** — only one rebuild runs at a time; a newer
    request cancels the previous so rapid preset toggling cannot pile up tasks
    or interleave writes.
  * **Error boundary** — a failed rebuild keeps the previous index serviceable
    and clears its in-flight state so the system never hangs in a degraded
    "empty" mode waiting on a dead task.

Retrieval degrades to an empty list whenever the index is stale or rebuilding,
so the caller answers from its other context sources instead of blocking.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lancedb
from filelock import FileLock, Timeout

from core.config.embedding_resolver import get_embedding_target
from core.memory.semantic_memory import _get_embedding
from shared.config import LANCEDB_PATH

logger = logging.getLogger("DOCS_INDEX")

# Repo root: core/memory/docs_index.py -> core -> ailienant-core -> repo root.
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
# Only the product "what / how" docs — never contributor/internal docs.
_CORPUS: Tuple[str, ...] = ("HowItWorks.md", "HowToUseIt.md", "README.md")

_TABLE_NAME: str = "ailienant_product_docs"
_META_PATH: str = os.path.join(LANCEDB_PATH, "_ailienant_docs_meta.json")
_LOCK_PATH: str = os.path.join(LANCEDB_PATH, "_ailienant_docs.lock")
_LOCK_TIMEOUT_S: float = 120.0

_CHUNK_CHARS: int = 1800
_CHUNK_MIN_CHARS: int = 200
_DEFAULT_K: int = 4

# Single-build guards.
_ASYNC_LOCK: asyncio.Lock = asyncio.Lock()
_current_rebuild_task: Optional["asyncio.Task[None]"] = None
_rebuild_in_flight: bool = False


# ── Corpus + versioning ────────────────────────────────────────────────

def _read_corpus() -> List[Tuple[str, str]]:
    """Read the present corpus files as (source_name, text). Missing files skipped."""
    out: List[Tuple[str, str]] = []
    for name in _CORPUS:
        fp = _REPO_ROOT / name
        try:
            if fp.is_file():
                text = fp.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    out.append((name, text))
        except OSError as exc:  # pragma: no cover — defensive
            logger.debug("docs corpus read failed for %s: %s", name, exc)
    return out


@lru_cache(maxsize=1)
def _corpus_content_hash() -> str:
    """Hash of the corpus bytes. Docs are static on disk for a given build."""
    h = hashlib.sha256()
    for name, text in _read_corpus():
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(text.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _current_version() -> Tuple[str, str]:
    """``(docs_content_hash, embedding_model_id)`` — the identity of a fresh index.

    The embedding id is read live (cheap, resolver-cached) so a preset switch is
    detected; the content hash is cached since the shipped docs don't change at runtime.
    """
    try:
        embed_id = get_embedding_target().model
    except Exception as exc:  # noqa: BLE001 — never let resolver I/O break retrieval
        logger.debug("embedding target resolve failed: %s", exc)
        embed_id = "unknown"
    return _corpus_content_hash(), embed_id


def _load_meta() -> Optional[Tuple[str, str]]:
    try:
        with open(_META_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data["content_hash"]), str(data["embedding_model_id"])
    except (OSError, KeyError, ValueError):
        return None


def _save_meta(version: Tuple[str, str]) -> None:
    os.makedirs(LANCEDB_PATH, exist_ok=True)
    tmp = _META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"content_hash": version[0], "embedding_model_id": version[1]}, fh)
    os.replace(tmp, _META_PATH)


def _index_is_fresh() -> bool:
    """True when the persisted meta matches the current corpus + embedding model."""
    return _load_meta() == _current_version()


# ── Chunking ───────────────────────────────────────────────────────────

def _chunk(text: str, source: str) -> List[Tuple[str, str]]:
    """Split a doc into heading/paragraph-aligned chunks (~_CHUNK_CHARS).

    Never splits inside a line; starts a fresh chunk at a markdown heading once
    the current one carries real content, and flushes when the size target is hit.
    """
    chunks: List[Tuple[str, str]] = []
    buf: List[str] = []
    size = 0
    idx = 0

    def flush() -> None:
        nonlocal buf, size, idx
        body = "\n".join(buf).strip()
        if len(body) >= _CHUNK_MIN_CHARS or (body and not chunks):
            chunks.append((f"{source}#{idx}", body))
            idx += 1
        buf = []
        size = 0

    for line in text.splitlines():
        is_heading = line.lstrip().startswith("#")
        if is_heading and size >= _CHUNK_MIN_CHARS:
            flush()
        buf.append(line)
        size += len(line) + 1
        if size >= _CHUNK_CHARS:
            flush()
    flush()
    return chunks


# ── Build (locked, idempotent) ─────────────────────────────────────────

def _write_table(records: List[Dict[str, Any]]) -> None:
    db = lancedb.connect(LANCEDB_PATH)
    db.create_table(_TABLE_NAME, data=records, mode="overwrite")


async def _build_index() -> None:
    """Embed every doc chunk and (over)write the table. Caller holds both locks."""
    corpus = _read_corpus()
    records: List[Dict[str, Any]] = []
    for source, text in corpus:
        for chunk_id, body in _chunk(text, source):
            try:
                vector = await _get_embedding(body)
            except Exception as exc:  # noqa: BLE001 — skip a bad chunk, never abort the build
                logger.warning("docs chunk embed failed (%s): %s", chunk_id, exc)
                continue
            records.append({"chunk_id": chunk_id, "source": source,
                            "content": body, "vector": vector})
    if not records:
        logger.warning("docs index build produced no records — skipping write.")
        return
    await asyncio.to_thread(_write_table, records)
    _save_meta(_current_version())
    logger.info("docs index built: %d chunks from %d docs.", len(records), len(corpus))


async def ensure_docs_index(*, force: bool = False) -> None:
    """Build the docs index if stale. Idempotent, loop-safe, cross-process-safe."""
    if not force and _index_is_fresh():
        return
    async with _ASYNC_LOCK:
        if not force and _index_is_fresh():
            return
        os.makedirs(LANCEDB_PATH, exist_ok=True)
        lock = FileLock(_LOCK_PATH, timeout=_LOCK_TIMEOUT_S)
        try:
            await asyncio.to_thread(lock.acquire)  # sync OS I/O off the event loop
        except Timeout:
            logger.warning("docs index lock busy — another process is building; skipping.")
            return
        try:
            if not force and _index_is_fresh():
                return
            await _build_index()
        finally:
            await asyncio.to_thread(lock.release)


# ── Background rebuild (cancellable, error-bounded) ────────────────────

async def _rebuild_task() -> None:
    me = asyncio.current_task()
    global _current_rebuild_task, _rebuild_in_flight
    try:
        await ensure_docs_index(force=True)
    except asyncio.CancelledError:
        raise  # superseded by a newer rebuild request
    except Exception as exc:  # noqa: BLE001 — keep prior index serviceable, allow retry
        logger.warning("docs index rebuild failed (prior index kept): %s", exc)
    finally:
        # Only clear if still the active task — a newer request may have replaced us.
        if _current_rebuild_task is me:
            _current_rebuild_task = None
            _rebuild_in_flight = False


def request_rebuild() -> None:
    """Dispatch a background (re)build, cancelling any in-flight one first."""
    global _current_rebuild_task, _rebuild_in_flight
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — no running loop (sync context)
        return
    if _current_rebuild_task is not None and not _current_rebuild_task.done():
        _current_rebuild_task.cancel()
    _rebuild_in_flight = True
    _current_rebuild_task = loop.create_task(_rebuild_task())


# ── Retrieval ──────────────────────────────────────────────────────────

def _query_table(vector: List[float], k: int) -> List[Tuple[str, str]]:
    db = lancedb.connect(LANCEDB_PATH)
    if _TABLE_NAME not in db.table_names():
        return []
    tbl = db.open_table(_TABLE_NAME)
    rows = tbl.search(vector).metric("cosine").limit(k).to_list()
    return [(str(r.get("source", "docs")), str(r.get("content", ""))) for r in rows if r.get("content")]


async def search_ailienant_docs(query: str, k: int = _DEFAULT_K) -> List[Tuple[str, str]]:
    """Top-k (source, chunk) docs for ``query``.

    Returns ``[]`` (graceful degradation) whenever the index is missing, stale,
    or rebuilding — and triggers a non-blocking background (re)build in that case.
    """
    if not query.strip():
        return []
    if not _index_is_fresh():
        request_rebuild()  # non-blocking; serve degraded this turn
        return []
    if _rebuild_in_flight:
        return []
    try:
        vector = await _get_embedding(query)
    except Exception as exc:  # noqa: BLE001 — retrieval must never break the analyst
        logger.debug("docs query embed failed (non-fatal): %s", exc)
        return []
    try:
        return await asyncio.to_thread(_query_table, vector, k)
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs query failed (non-fatal): %s", exc)
        return []
