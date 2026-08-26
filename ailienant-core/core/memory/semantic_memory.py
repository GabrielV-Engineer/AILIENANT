# core/memory/semantic_memory.py
"""Vector Memory Engine (LanceDB multi-tenancy & semantic upsert).

Embeds every successfully indexed file into a shared LanceDB table
(workspace_embeddings), isolated per workspace via workspace_hash.

semantic_upsert  — called by the reactive indexer after each file is indexed.
search           — computes ContextMeter.semantic_similarity for routing.

Blocking LanceDB operations run inside asyncio.to_thread.
Embedding generation uses litellm.aembedding() (already async).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import tiktoken

from core.config.embedding_resolver import get_embedding_target
from core.storage_paths import graphrag_lancedb_path

logger = logging.getLogger("SEMANTIC_MEMORY")

# Module-level tiktoken singleton — loaded once at import time (reading the BPE
# file from disk is a one-time cost; never instantiate inside a hot path).
_ENC: tiktoken.Encoding = tiktoken.get_encoding("cl100k_base")

_EMBEDDING_DIM: int = int(os.getenv("AILIENANT_EMBEDDING_DIM", "1536"))
_TABLE_NAME: str = "workspace_embeddings"
_TOP_K: int = 5
_MIN_TOKENS: int = 100        # Anti-fragmentation gate
_HNSW_MIN_ROWS: int = 256     # IVF training minimum with num_partitions=1

# ── Symbol-level chunking ─────────────────────────────────────────────────
# One vector per whole file makes a large multi-function module collapse into a
# single centroid that resembles none of its functions, capping retrieval
# precision independently of which embedding model is configured. The store is
# therefore hybrid by size: every file keeps its file-level vector unchanged, and
# files above _CHUNK_FILE_MIN_TOKENS ADDITIONALLY emit one vector per symbol into
# a separate table. The file table's Arrow schema is never mutated, so a corpus
# indexed before chunking existed simply has no chunk rows and every consumer
# falls back to file-level behavior.
_CHUNK_TABLE_NAME: str = "symbol_chunk_embeddings"
_CHUNK_FILE_MIN_TOKENS: int = int(os.getenv("AILIENANT_CHUNK_FILE_MIN_TOKENS", "800"))
# Per-chunk anti-fragmentation floor. Deliberately far below _MIN_TOKENS: that
# gate is calibrated for whole files, and most individual functions fall under
# 100 tokens, so reusing it here would silently write nothing at all. This floor
# only drops one-line accessors, whose vectors are noise.
_CHUNK_MIN_TOKENS: int = 20
_CHUNK_MAX_PER_FILE: int = 200      # fan-out bound for a pathological file
_CHUNK_TEXT_MAX_CHARS: int = 4000   # stored-evidence cap (~1000 tokens)
# Read-side injection budget per file when several chunks of one file match.
# Sized against the pre-chunking evidence ceiling (_TOP_K x the AST skeleton cap
# of 1500 bytes = 7500 chars); a larger per-file budget multiplies by _TOP_K and
# would blow the context window. To surface more evidence per file, lower _TOP_K
# rather than raising this.
_MAX_EVIDENCE_CHARS_PER_FILE: int = 2000

# Batched embedding. A single request carrying every symbol of a large file risks
# HTTP 413 or silent array truncation on OpenAI-compatible local providers, so
# requests are partitioned by BOTH item count and cumulative token payload —
# 32 large functions can breach a payload ceiling while still being only 32 items.
_EMBED_BATCH_SIZE: int = 32
_EMBED_CONCURRENCY: int = 4   # bounded so a local provider is not thrashed

# Load-bearing ordering: semantic_upsert returns early below _MIN_TOKENS, before
# any chunking is considered. Chunking is therefore only reachable while the file
# gate sits above it — otherwise a file could qualify for chunks yet never be
# embedded at all, and its chunk rows would violate the chunks-subset-of-files
# invariant that search_snippets' corpus-presence short-circuit relies on.
assert _CHUNK_FILE_MIN_TOKENS > _MIN_TOKENS, (
    "_CHUNK_FILE_MIN_TOKENS must exceed _MIN_TOKENS; otherwise a file can emit "
    "chunk rows without ever receiving a file-level embedding."
)
# The embed-input ceiling itself is provider-specific and resolved per call from
# get_embedding_target().max_input_tokens (core/config/byom_config.py::EmbeddingTarget),
# whose default (8191, the ada-002-family limit) is the fallback when no BYOM target
# is configured — see semantic_upsert.

# Strict allowlist — prevents SQL injection in the native .where() predicate.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# ── Corpus-presence probe cache ───────────────────────────────────────────
# A cold/empty workspace must not be mistaken for a rich-but-low-coverage one by
# the router. is_corpus_empty answers "does this workspace have any indexed rows?"
# cheaply and is consulted once per planner turn, so a short-TTL module-level cache
# (shared across the stateless, per-call manager instances) avoids a redundant
# LanceDB round-trip every turn. Keyed by (lancedb_path, workspace_hash); the lock
# is never held across an await; entries are invalidated on every corpus write.
_CORPUS_PRESENCE_TTL_S: float = 30.0
_corpus_presence_cache: Dict[Tuple[str, str], Tuple[float, bool]] = {}
_corpus_presence_lock = threading.Lock()

_WORKSPACE_SCHEMA: pa.Schema = pa.schema([
    pa.field("file_path",       pa.utf8()),
    pa.field("workspace_hash",  pa.utf8()),   # project_id (SHA-256) isolation key
    pa.field("content_snippet", pa.utf8()),   # first 500 chars for audit/debug
    pa.field("token_count",     pa.int32()),
    pa.field("vector",          pa.list_(pa.float32(), list_size=_EMBEDDING_DIM)),
    pa.field("indexed_at",      pa.utf8()),   # ISO-8601 UTC timestamp
])


def _chunk_schema_for_dim(dim: int) -> pa.Schema:
    """Schema for the per-symbol chunk table at a concrete embedding dimension.

    ``chunk_text`` is both the stored retrieval evidence and the exact embed
    input, and ``content_hash`` digests it — so a hash match guarantees the
    stored vector still describes that text and can be reused verbatim.
    """
    return pa.schema([
        pa.field("file_path",      pa.utf8()),
        pa.field("workspace_hash", pa.utf8()),
        pa.field("qualified_name", pa.utf8()),   # dotted FQN (module -> class -> method)
        pa.field("kind",           pa.utf8()),   # function | method
        pa.field("start_line",     pa.int32()),  # 1-indexed, inclusive
        pa.field("end_line",       pa.int32()),  # 1-indexed, inclusive
        pa.field("chunk_text",     pa.utf8()),
        pa.field("content_hash",   pa.utf8()),   # sha256(chunk_text) — vector reuse key
        pa.field("token_count",    pa.int32()),
        pa.field("vector",         pa.list_(pa.float32(), list_size=dim)),
        pa.field("indexed_at",     pa.utf8()),
    ])


class SemanticMemoryManager:
    """Async LanceDB-backed per-file semantic store.

    Stateless — safe to share across concurrent LangGraph fan-out invocations.
    """

    def __init__(self, lancedb_path: Optional[str] = None) -> None:
        # The GraphRAG store is partitioned per project; resolve the bound
        # project's directory when no explicit path is supplied. Resolved at
        # instantiation (not import) so each session uses its own partition.
        self._lancedb_path = lancedb_path or graphrag_lancedb_path()

    # ── Public API ────────────────────────────────────────────────────

    async def semantic_upsert(
        self,
        file_path: str,
        content: str,
        workspace_hash: str,
        symbols: Optional[Sequence[Any]] = None,
    ) -> bool:
        """Embed a file and upsert into workspace_embeddings.

        ``symbols`` is optional and additive: when supplied for a file above
        _CHUNK_FILE_MIN_TOKENS, per-symbol chunk vectors are ALSO written to the
        chunk table. Callers embedding non-source text (consolidation notes, for
        instance) simply omit it and get the unchanged file-level behavior. The
        chunk table is a pure accelerator, so a chunk failure never changes this
        function's return value — see below.

        No-op if content has fewer than _MIN_TOKENS tokens (anti-fragmentation).
        Truncates to the active embedding target's max_input_tokens ceiling via a
        tiktoken round-trip to avoid splitting multibyte characters (never slices
        raw UTF-8 bytes). A truncation is logged (path, real count, ceiling, tokens
        dropped) — the dropped tail is otherwise invisible to vector search with no
        trace of the loss.

        Returns True on a successful write or an intentional skip (too few tokens),
        and False when embedding or the LanceDB write fails. The reactive indexer
        uses this signal to drive its circuit breaker — an intentional skip must
        not be mistaken for a backend outage.
        """
        tokens_enc = _ENC.encode(content)
        token_count = len(tokens_enc)
        if token_count < _MIN_TOKENS:
            logger.debug(
                "SemanticMemory: skip %s — only %d tokens (< %d).",
                file_path, token_count, _MIN_TOKENS,
            )
            return True

        hash_valid = bool(_SAFE_ID_RE.match(workspace_hash)) if workspace_hash else False
        if workspace_hash and not hash_valid:
            logger.warning(
                "SemanticMemory: workspace_hash %r failed sanitization — delete step skipped.",
                workspace_hash,
            )

        # Ceiling is provider-specific (resolved from the active BYOM embedding target);
        # _MAX_EMBED_TOKENS is only the fallback default when no target is configured.
        # cl100k_base is a deliberate conservative proxy here — it is OpenAI's own BPE
        # vocabulary, not the active provider's tokenizer (e.g. Ollama's WordPiece), so
        # this measurement can under- or over-count by a small margin. A per-provider
        # tokenizer would pull in a new dependency per provider for a marginal accuracy
        # gain (charter §9); the conservative proxy is preferred until that trade flips.
        ceiling = get_embedding_target().max_input_tokens
        if token_count > ceiling:
            logger.warning(
                "SemanticMemory: truncating %s for embedding — %d tokens exceeds the "
                "%d-token ceiling (%d tokens dropped, content past the cut is invisible "
                "to vector search).",
                file_path, token_count, ceiling, token_count - ceiling,
            )
        safe_content: str = (
            _ENC.decode(tokens_enc[:ceiling]) if token_count > ceiling else content
        )

        try:
            vector = await _get_embedding(safe_content)
        except Exception as embed_err:
            logger.warning("SemanticMemory: embedding failed (non-fatal): %s", embed_err)
            return False

        record: Dict[str, Any] = {
            "file_path":       file_path,
            "workspace_hash":  workspace_hash,
            "content_snippet": content[:500],
            "token_count":     token_count,
            "vector":          vector,
            "indexed_at":      datetime.now(timezone.utc).isoformat(),
        }

        try:
            await asyncio.to_thread(
                self._write_record, record, workspace_hash, file_path, hash_valid
            )
            logger.debug("SemanticMemory: upserted %s (workspace=%s)", file_path, workspace_hash)
        except Exception as write_err:
            logger.warning("SemanticMemory: write failed (non-fatal): %s", write_err)
            return False

        # Chunk rows are written only after the file-level row is committed, which
        # keeps the chunks-subset-of-files invariant that the corpus-presence
        # short-circuit in search_snippets depends on. A failure here is contained:
        # retrieval degrades to file-level evidence, which is the older-but-correct
        # path, so it must not flip the return value the reactive indexer's circuit
        # breaker reads — an accelerator fault is not a backend outage.
        if symbols and hash_valid and token_count >= _CHUNK_FILE_MIN_TOKENS:
            try:
                await self._write_chunks(file_path, content, workspace_hash, symbols)
            except Exception as chunk_err:  # noqa: BLE001 — contained; file-level row already stands
                logger.warning(
                    "SemanticMemory: chunk write failed for %s (non-fatal, "
                    "retrieval falls back to file-level evidence): %s",
                    file_path, chunk_err, exc_info=True,
                )
        return True

    # ── Symbol-level chunk writes ─────────────────────────────────────

    @staticmethod
    def _build_chunks(
        content: str, symbols: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        """Slice a file's function/method bodies into embeddable chunk records.

        Classes are deliberately excluded: ``collect_symbol_defs`` emits a class
        AND its methods, and the class range fully contains each method range, so
        embedding both would pay twice for the same bytes and double-count the
        same code in retrieval. A class header's semantics already ride in the
        file-level vector.

        Note that a symbol's range anchors on the definition node, so a decorator
        sitting above it is not part of the chunk — the body carries the meaning.
        """
        lines = content.splitlines()
        out: List[Dict[str, Any]] = []
        for sym in symbols:
            if len(out) >= _CHUNK_MAX_PER_FILE:
                break
            if sym.kind not in ("function", "method"):
                continue
            start, end = sym.start_line, sym.end_line
            if start < 1 or end < start or start > len(lines):
                continue
            text = "\n".join(lines[start - 1:end]).strip()
            if not text:
                continue  # malformed range — never send an empty string to embed
            text = text[:_CHUNK_TEXT_MAX_CHARS]
            token_count = len(_ENC.encode(text))
            if token_count < _CHUNK_MIN_TOKENS:
                continue
            out.append({
                "qualified_name": sym.qualified_name,
                "kind":           sym.kind,
                "start_line":     start,
                "end_line":       end,
                "chunk_text":     text,
                "content_hash":   hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "token_count":    token_count,
            })
        return out

    async def _write_chunks(
        self,
        file_path: str,
        content: str,
        workspace_hash: str,
        symbols: Sequence[Any],
        build_index: bool = True,
    ) -> int:
        """Replace this file's chunk rows. Returns the number of rows written.

        Only chunks whose text actually changed are embedded: the file's stored
        (content_hash -> vector) pairs are reused for everything else. Keying
        reuse on the text digest rather than on (qualified_name, start_line) is
        load-bearing — line numbers shift whenever anything above a function is
        edited, so a positional key would mark every chunk dirty after a one-line
        insert and re-embed the whole file, which is exactly the cost this avoids.

        All-or-nothing: an embedding failure aborts before any write, leaving the
        previous rows intact rather than publishing a partial symbol set that
        would masquerade as complete evidence.
        """
        chunks = self._build_chunks(content, symbols)
        if not chunks:
            # A file that no longer yields chunks must not keep stale ones.
            await asyncio.to_thread(self._delete_chunk_rows, file_path, workspace_hash)
            return 0

        reusable: Dict[str, List[float]] = await asyncio.to_thread(
            self._existing_chunk_vectors, file_path, workspace_hash
        )
        pending = [c for c in chunks if c["content_hash"] not in reusable]
        if pending:
            vectors = await _get_embeddings([c["chunk_text"] for c in pending])
            for chunk, vector in zip(pending, vectors):
                reusable[chunk["content_hash"]] = vector
        logger.debug(
            "SemanticMemory: %s — %d chunk(s), %d embedded, %d reused.",
            file_path, len(chunks), len(pending), len(chunks) - len(pending),
        )

        now = datetime.now(timezone.utc).isoformat()
        records: List[Dict[str, Any]] = [
            {
                **chunk,
                "file_path":      file_path,
                "workspace_hash": workspace_hash,
                "vector":         reusable[chunk["content_hash"]],
                "indexed_at":     now,
            }
            for chunk in chunks
        ]
        await asyncio.to_thread(
            self._write_chunk_records, records, file_path, workspace_hash, build_index
        )
        return len(records)

    def _scan_chunk_table(
        self, columns: List[str], workspace_hash: str, file_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Bounded, injection-proof column scan of the chunk table.

        Mirrors ``_dump_vectors_sync``'s pushdown-with-fallback shape: a PyArrow
        Expression pushdown (never an interpolated SQL string) when the optional
        ``pylance`` extra is importable, else a bounded full-table Arrow read
        filtered in Python. ``pylance``'s absence must degrade this to a slower
        scan, never raise — content-addressed reuse (finding 11) and the vector
        GC both depend on this never crashing their caller.
        """
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _CHUNK_TABLE_NAME not in db.table_names():
            return []
        tbl = db.open_table(_CHUNK_TABLE_NAME)
        expr = pc.field("workspace_hash") == workspace_hash  # pyright: ignore[reportAttributeAccessIssue]
        if file_path is not None:
            expr = expr & (pc.field("file_path") == file_path)  # pyright: ignore[reportAttributeAccessIssue]
        try:
            ds = tbl.to_lance()
            try:
                arrow_tbl = ds.to_table(columns=columns, filter=expr)
            except (TypeError, AttributeError):
                arrow_tbl = ds.scanner(columns=columns, filter=expr).to_table()
            return arrow_tbl.to_pylist()
        except Exception as err:  # noqa: BLE001 — pushdown is an optimisation, not a correctness gate
            logger.debug("chunk-table pushdown unavailable (%s) — bounded fallback.", err)
            rows = tbl.to_arrow().to_pylist()
            return [
                r for r in rows
                if str(r.get("workspace_hash", "")) == workspace_hash
                and (file_path is None or str(r.get("file_path", "")) == file_path)
            ]

    def _existing_chunk_vectors(
        self, file_path: str, workspace_hash: str,
    ) -> Dict[str, List[float]]:
        """Stored (content_hash -> vector) pairs for one file. {} when absent."""
        rows = self._scan_chunk_table(["content_hash", "vector"], workspace_hash, file_path)
        return {
            str(r["content_hash"]): list(r["vector"])
            for r in rows
            if r.get("content_hash") and r.get("vector")
        }

    def _write_chunk_records(
        self,
        records: List[Dict[str, Any]],
        file_path: str,
        workspace_hash: str,
        build_index: bool,
    ) -> None:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        vec_dim = len(records[0]["vector"])
        schema = _chunk_schema_for_dim(vec_dim)
        if _CHUNK_TABLE_NAME in db.table_names():
            tbl = db.open_table(_CHUNK_TABLE_NAME)
            existing_dim = self._table_vector_dim(tbl)
            if existing_dim is not None and existing_dim != vec_dim:
                logger.warning(
                    "SemanticMemory: chunk embedding dim changed %d → %d — recreating table.",
                    existing_dim, vec_dim,
                )
                db.drop_table(_CHUNK_TABLE_NAME)
                tbl = db.create_table(_CHUNK_TABLE_NAME, schema=schema)
        else:
            tbl = db.create_table(_CHUNK_TABLE_NAME, schema=schema)

        safe_path = file_path.replace("'", "''")  # standard SQL single-quote escape
        tbl.delete(f"workspace_hash = '{workspace_hash}' AND file_path = '{safe_path}'")
        tbl.add(records)
        if build_index:
            self._build_chunk_index(tbl)

    @staticmethod
    def _build_chunk_index(tbl: Any) -> None:
        """(Re)build the chunk table's ANN index; a miss only costs a slower scan."""
        try:
            tbl.create_index(
                vector_column_name="vector",
                index_type="IVF_HNSW_SQ",
                metric="cosine",
                num_partitions=1,
                m=20,
                ef_construction=300,
                replace=True,
            )
        except Exception as idx_err:
            logger.debug(
                "Chunk HNSW index deferred (table likely too small, need %d rows): %s",
                _HNSW_MIN_ROWS,
                idx_err,
            )

    def _delete_chunk_rows(self, file_path: str, workspace_hash: str) -> None:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _CHUNK_TABLE_NAME not in db.table_names():
            return
        tbl = db.open_table(_CHUNK_TABLE_NAME)
        safe_path = file_path.replace("'", "''")
        tbl.delete(f"workspace_hash = '{workspace_hash}' AND file_path = '{safe_path}'")

    async def semantic_delete(self, file_path: str, workspace_hash: str) -> None:
        """Evict a single file's vector from workspace_embeddings (reactive purge).

        Counterpart to the Memory Janitor's bulk GC: when a file is deleted or
        renamed the reactive path calls this so the stale vector cannot pollute
        RAG results before the next manual janitor run. Sanitizes workspace_hash
        against the allowlist and escapes the path exactly as _write_record does.
        Non-fatal on any error — a failed eviction must never break the WS loop.
        """
        if not workspace_hash or not _SAFE_ID_RE.match(workspace_hash):
            logger.warning(
                "SemanticMemory.semantic_delete: workspace_hash %r failed sanitization — skipped.",
                workspace_hash,
            )
            return
        try:
            await asyncio.to_thread(self._delete_record, file_path, workspace_hash)
            logger.debug("SemanticMemory: evicted %s (workspace=%s)", file_path, workspace_hash)
        except Exception as del_err:
            logger.warning("SemanticMemory: delete failed (non-fatal): %s", del_err)

    def _delete_record(self, file_path: str, workspace_hash: str) -> None:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        safe_path = file_path.replace("'", "''")  # standard SQL single-quote escape
        predicate = f"workspace_hash = '{workspace_hash}' AND file_path = '{safe_path}'"
        names = db.table_names()
        # Both tables, always: evicting only the file-level row would strand the
        # file's chunk rows as ghosts that keep surfacing in retrieval.
        for table_name in (_TABLE_NAME, _CHUNK_TABLE_NAME):
            if table_name in names:
                db.open_table(table_name).delete(predicate)
        self._invalidate_corpus_presence(workspace_hash)

    # ── Corpus-presence probe ─────────────────────────────────────────
    async def is_corpus_empty(self, workspace_hash: str) -> bool:
        """True when the workspace has no indexed rows (nothing to retrieve from).

        Lets the router distinguish "no corpus" from "rich corpus, low coverage":
        only the latter warrants escalating to CLOUD. Short-TTL cached and
        invalidated on every corpus write, so a cold workspace pays one cheap
        count per TTL rather than per turn.

        A blank or non-allowlisted workspace_hash returns False (treated as
        non-empty) so the conservative CLOUD escalation is never dropped by
        accident — the probe must never be the reason a low-CSS turn stays local.
        """
        if not workspace_hash or not _SAFE_ID_RE.match(workspace_hash):
            return False

        key = (self._lancedb_path, workspace_hash)
        now = time.monotonic()

        # Fast path: fresh cache hit. Lock is released before the await below.
        with _corpus_presence_lock:
            cached = _corpus_presence_cache.get(key)
            if cached is not None and (now - cached[0]) <= _CORPUS_PRESENCE_TTL_S:
                return cached[1]

        empty = await asyncio.to_thread(self._is_corpus_empty_sync, workspace_hash)

        # Double-checked locking: a concurrent caller may have populated a fresh
        # entry while we were off-thread — prefer it, then store ours otherwise.
        with _corpus_presence_lock:
            cached = _corpus_presence_cache.get(key)
            after = time.monotonic()
            if cached is not None and (after - cached[0]) <= _CORPUS_PRESENCE_TTL_S:
                return cached[1]
            _corpus_presence_cache[key] = (after, empty)
            return empty

    def _is_corpus_empty_sync(self, workspace_hash: str) -> bool:
        """Blocking row-count probe for the workspace. Runs inside to_thread."""
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return True
        tbl = db.open_table(_TABLE_NAME)
        # Per-workspace filter is essential: the table is shared across workspaces,
        # so an unfiltered count would mis-report a fresh workspace as non-empty
        # whenever any other workspace holds rows.
        count: int = tbl.count_rows(filter=f"workspace_hash = '{workspace_hash}'")
        return count == 0

    def _invalidate_corpus_presence(self, workspace_hash: str) -> None:
        """Drop the cached presence verdict after a corpus mutation."""
        with _corpus_presence_lock:
            _corpus_presence_cache.pop((self._lancedb_path, workspace_hash), None)

    async def search(
        self,
        user_input: str,
        workspace_hash: str = "",
        k: int = _TOP_K,
    ) -> float:
        """Return aggregated semantic similarity score in [0.0, 1.0].

        Queries workspace_embeddings for files most similar to user_input.
        Converts LanceDB cosine distances (0=identical, 1=opposite) to
        similarities and averages top-k. Returns 0.0 on any failure or empty input.
        """
        if not user_input.strip():
            return 0.0

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:
            logger.warning("SemanticMemory.search: embed failed (non-fatal): %s", embed_err)
            return 0.0

        try:
            distances: List[float] = await asyncio.to_thread(
                self._query_records, vector, workspace_hash, k
            )
        except Exception as query_err:
            logger.warning("SemanticMemory.search: query failed (non-fatal): %s", query_err)
            return 0.0

        if not distances:
            return 0.0

        avg: float = sum(max(0.0, 1.0 - d) for d in distances) / len(distances)
        return min(1.0, max(0.0, avg))

    # ── Blocking helpers (asyncio.to_thread) ──────────────────────────

    @staticmethod
    def _schema_for_dim(dim: int) -> pa.Schema:
        """Build the workspace schema for a concrete embedding dimension."""
        return pa.schema([
            pa.field("file_path",       pa.utf8()),
            pa.field("workspace_hash",  pa.utf8()),
            pa.field("content_snippet", pa.utf8()),
            pa.field("token_count",     pa.int32()),
            pa.field("vector",          pa.list_(pa.float32(), list_size=dim)),
            pa.field("indexed_at",      pa.utf8()),
        ])

    @staticmethod
    def _table_vector_dim(tbl: Any) -> Optional[int]:
        """Return the fixed vector dimension of an existing table, or None."""
        try:
            return int(tbl.schema.field("vector").type.list_size)
        except Exception:
            return None

    def _write_record(
        self,
        record: Dict[str, Any],
        workspace_hash: str,
        file_path: str,
        hash_valid: bool,
    ) -> None:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        # Provider-agnostic dimension safety: the real vector length wins. If the
        # active embedding provider changed (e.g. 1536 → 768), drop & recreate the
        # table so heterogeneous-dim vectors never collide in one schema.
        vec_dim = len(record["vector"])
        schema = self._schema_for_dim(vec_dim)
        if _TABLE_NAME in db.table_names():
            tbl = db.open_table(_TABLE_NAME)
            existing_dim = self._table_vector_dim(tbl)
            if existing_dim is not None and existing_dim != vec_dim:
                logger.warning(
                    "SemanticMemory: embedding dim changed %d → %d — recreating tables.",
                    existing_dim, vec_dim,
                )
                db.drop_table(_TABLE_NAME)
                tbl = db.create_table(_TABLE_NAME, schema=schema)
                # The chunk table shares the provider's dimension, so leaving it
                # behind would keep stale-dim vectors that fail every subsequent
                # search. Recreated lazily on the next chunk write.
                if _CHUNK_TABLE_NAME in db.table_names():
                    db.drop_table(_CHUNK_TABLE_NAME)
        else:
            tbl = db.create_table(_TABLE_NAME, schema=schema)

        if hash_valid:
            safe_path = file_path.replace("'", "''")  # standard SQL single-quote escape
            tbl.delete(f"workspace_hash = '{workspace_hash}' AND file_path = '{safe_path}'")

        tbl.add([record])
        self._invalidate_corpus_presence(workspace_hash)

        try:
            tbl.create_index(
                vector_column_name="vector",
                index_type="IVF_HNSW_SQ",
                metric="cosine",
                num_partitions=1,
                m=20,
                ef_construction=300,
                replace=True,
            )
        except Exception as idx_err:
            logger.debug(
                "HNSW index deferred (table likely too small, need %d rows): %s",
                _HNSW_MIN_ROWS,
                idx_err,
            )

    def _query_records(
        self,
        vector: List[float],
        workspace_hash: str,
        k: int,
    ) -> List[float]:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []

        # `tbl: Any` — the lancedb stub omits LanceQueryBuilder.metric; the runtime
        # method exists. Annotating the handle avoids a false reportAttributeAccessIssue
        # without masking real typing on the surrounding code.
        tbl: Any = db.open_table(_TABLE_NAME)
        query = tbl.search(vector).metric("cosine").limit(k)

        # Pre-filter pushdown: DataFusion applies the predicate during HNSW
        # traversal, guaranteeing true O(log N) latency and full recall within
        # the workspace domain. Skipped if workspace_hash is empty or fails the
        # allowlist check — never inject unsanitized input.
        if workspace_hash and _SAFE_ID_RE.match(workspace_hash):
            query = query.where(f"workspace_hash = '{workspace_hash}'")
        elif workspace_hash:
            logger.warning(
                "SemanticMemory: workspace_hash %r failed sanitization — filter skipped.",
                workspace_hash,
            )

        rows: List[Any] = query.to_list()
        return [float(r.get("_distance", 1.0)) for r in rows]

    def _query_records_with_paths(
        self,
        vector: List[float],
        workspace_hash: str,
        k: int,
    ) -> List[Tuple[str, float, str]]:
        """Like _query_records but returns (file_path, distance, indexed_at) triples.

        ``indexed_at`` rides out of this single query (it is already a column on
        every row) so the recency meter never needs a second DB round-trip.
        """
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []

        # `tbl: Any` — the lancedb stub omits LanceQueryBuilder.metric; the runtime
        # method exists. Annotating the handle avoids a false reportAttributeAccessIssue
        # without masking real typing on the surrounding code.
        tbl: Any = db.open_table(_TABLE_NAME)
        query = tbl.search(vector).metric("cosine").limit(k)

        if workspace_hash and _SAFE_ID_RE.match(workspace_hash):
            query = query.where(f"workspace_hash = '{workspace_hash}'")
        elif workspace_hash:
            logger.warning(
                "SemanticMemory: workspace_hash %r failed sanitization — filter skipped.",
                workspace_hash,
            )

        rows: List[Any] = query.to_list()
        return [
            (
                str(r.get("file_path", "")),
                float(r.get("_distance", 1.0)),
                str(r.get("indexed_at", "")),
            )
            for r in rows
            if r.get("file_path")
        ]

    # ── Phase 3.2: combined search (single embedding call) ────────────

    async def search_with_paths(
        self,
        user_input: str,
        workspace_hash: str = "",
        k: int = _TOP_K,
    ) -> Tuple[float, List[str], List[str]]:
        """Single embedding call returns (aggregated_score, top_k_file_paths, indexed_at).

        Avoids the double embedding call that separate search() + search_files()
        would require. The third element carries each retrieved file's ISO
        ``indexed_at`` (parallel to file_paths) so the recency meter gets a
        time signal without a second query. Returns (0.0, [], []) on empty input,
        an empty corpus (nothing to retrieve), or any failure.
        """
        if not user_input.strip():
            return 0.0, [], []

        # Cold/empty workspace: nothing to retrieve, so skip the embedding round-trip.
        # An empty store returns [] from the query path anyway — this only drops the
        # wasted backend call. is_corpus_empty is short-TTL cached and returns False for
        # a blank/unsafe hash, so the safe default never skips a real search.
        if await self.is_corpus_empty(workspace_hash):
            return 0.0, [], []

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:
            logger.warning(
                "SemanticMemory.search_with_paths: embed failed (non-fatal): %s", embed_err
            )
            return 0.0, [], []

        try:
            triples: List[Tuple[str, float, str]] = await asyncio.to_thread(
                self._query_records_with_paths, vector, workspace_hash, k
            )
        except Exception as query_err:
            logger.warning(
                "SemanticMemory.search_with_paths: query failed (non-fatal): %s", query_err
            )
            return 0.0, [], []

        if not triples:
            return 0.0, [], []

        avg = sum(max(0.0, 1.0 - d) for _, d, _ in triples) / len(triples)
        score = min(1.0, max(0.0, avg))
        file_paths = [fp for fp, _, _ in triples]
        indexed_at = [ts for _, _, ts in triples]
        return score, file_paths, indexed_at

    # ── Snippet retrieval for live-chat GraphRAG injection ─────

    def _query_snippets(
        self, vector: List[float], workspace_hash: str, k: int
    ) -> List[Tuple[str, str]]:
        """Return (file_path, content_snippet) pairs for the top-k nearest vectors."""
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []

        # `tbl: Any` — the lancedb stub omits LanceQueryBuilder.metric; the runtime
        # method exists. Annotating the handle avoids a false reportAttributeAccessIssue
        # without masking real typing on the surrounding code.
        tbl: Any = db.open_table(_TABLE_NAME)
        query = tbl.search(vector).metric("cosine").limit(k)

        if workspace_hash and _SAFE_ID_RE.match(workspace_hash):
            query = query.where(f"workspace_hash = '{workspace_hash}'")
        elif workspace_hash:
            logger.warning(
                "SemanticMemory: workspace_hash %r failed sanitization — filter skipped.",
                workspace_hash,
            )

        rows: List[Any] = query.to_list()
        return [
            (str(r.get("file_path", "")), str(r.get("content_snippet", "")))
            for r in rows
            if r.get("file_path")
        ]

    def _query_chunks(
        self, vector: List[float], workspace_hash: str, k: int
    ) -> List[Tuple[str, float, str]]:
        """Return (file_path, distance, chunk_text) for the top-k nearest symbols.

        Returns [] when the chunk table does not exist — the normal state for a
        corpus indexed before symbol chunking, and the reason every caller must
        treat chunk evidence as an optional upgrade rather than a requirement.
        """
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _CHUNK_TABLE_NAME not in db.table_names():
            return []

        # `tbl: Any` — the lancedb stub omits LanceQueryBuilder.metric; the runtime
        # method exists. Annotating the handle avoids a false reportAttributeAccessIssue
        # without masking real typing on the surrounding code.
        tbl: Any = db.open_table(_CHUNK_TABLE_NAME)
        query = tbl.search(vector).metric("cosine").limit(k)

        if workspace_hash and _SAFE_ID_RE.match(workspace_hash):
            query = query.where(f"workspace_hash = '{workspace_hash}'")
        elif workspace_hash:
            logger.warning(
                "SemanticMemory: workspace_hash %r failed sanitization — filter skipped.",
                workspace_hash,
            )

        rows: List[Any] = query.to_list()
        return [
            (
                str(r.get("file_path", "")),
                float(r.get("_distance", 1.0)),
                str(r.get("chunk_text", "")),
            )
            for r in rows
            if r.get("file_path") and r.get("chunk_text")
        ]

    async def search_snippets(
        self,
        user_input: str,
        workspace_hash: str = "",
        k: int = _TOP_K,
        project_root: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """Return (file_path, evidence_snippet) pairs most relevant to user_input.

        Powers invisible GraphRAG context injection into the live chat system
        prompt (and the MCP ``query_memory`` tool). Returns [] on empty input, an
        empty corpus (nothing to retrieve), or any failure (non-fatal).

        The second element of each pair is, where possible, an AST skeleton of the
        WHOLE matched file (signatures + docstrings, bodies elided) rather than the
        stored ``content_snippet`` — see ``_distill_snippets`` for why: the stored
        value is a fixed 500-char head-of-file slice meant for dashboard audit/debug,
        not retrieval evidence, and a file that matched on line 400 would otherwise
        contribute only its import header. ``project_root`` is optional and additive
        — omitting it degrades gracefully to the raw ``content_snippet`` for every
        result (unchanged pre-fix behavior), since the VFS firewall needs a root to
        resolve relative paths and apply ignore rules.
        """
        if not user_input.strip():
            return []

        # Cold/empty workspace: skip the embedding round-trip — the query path would
        # return [] anyway. Cached presence probe; safe default (False) on a blank hash.
        if await self.is_corpus_empty(workspace_hash):
            return []

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:  # noqa: BLE001 — RAG must never break a chat turn
            logger.warning("SemanticMemory.search_snippets: embed failed (non-fatal): %s", embed_err)
            return []

        # Both lookups overlap rather than running back-to-back: this is the
        # evidence hot path, so total latency stays max(file, chunk) instead of
        # their sum. Exceptions are captured per-side so one store failing still
        # yields the other's results.
        file_res, chunk_res = await asyncio.gather(
            asyncio.to_thread(self._query_snippets, vector, workspace_hash, k),
            asyncio.to_thread(self._query_chunks, vector, workspace_hash, k),
            return_exceptions=True,
        )

        if isinstance(file_res, BaseException):
            logger.warning(
                "SemanticMemory.search_snippets: file query failed (non-fatal): %s",
                file_res, exc_info=file_res,
            )
            raw_pairs: List[Tuple[str, str]] = []
        else:
            raw_pairs = file_res

        if isinstance(chunk_res, BaseException):
            logger.warning(
                "SemanticMemory.search_snippets: chunk query failed (non-fatal, "
                "degrading to file-level evidence): %s",
                chunk_res, exc_info=chunk_res,
            )
            chunk_hits: List[Tuple[str, float, str]] = []
        else:
            chunk_hits = chunk_res

        if not raw_pairs and not chunk_hits:
            return []

        return await self._merge_evidence(raw_pairs, chunk_hits, project_root, k)

    async def _merge_evidence(
        self,
        file_pairs: List[Tuple[str, str]],
        chunk_hits: List[Tuple[str, float, str]],
        project_root: Optional[str],
        k: int,
    ) -> List[Tuple[str, str]]:
        """Fuse file-level and symbol-level hits into one evidence list per file.

        Distances from the two tables are directly comparable — same model, same
        cosine metric, same dimension — so nearest-first ordering across both is
        meaningful. Results are deduped to file granularity because consumers
        render one card per file; a file matching on several of its symbols keeps
        all of them (bounded) rather than crowding other files out of the top-k.
        """
        by_file: Dict[str, List[Tuple[float, str]]] = {}
        order: List[str] = []
        for file_path, distance, text in sorted(chunk_hits, key=lambda h: h[1]):
            if file_path not in by_file:
                by_file[file_path] = []
                order.append(file_path)
            by_file[file_path].append((distance, text))

        # File-level hits fill any remaining slots, preserving their own ranking.
        for file_path, _ in file_pairs:
            if file_path not in by_file:
                by_file[file_path] = []
                order.append(file_path)

        fallbacks = dict(file_pairs)
        out: List[Tuple[str, str]] = []
        for file_path in order[:k]:
            evidence = self._pack_chunk_evidence(by_file[file_path])
            if evidence:
                out.append((file_path, evidence))
            else:
                # No stored symbol evidence for this file (it is under the
                # chunking threshold, or predates the chunk table) — fall back to
                # distilling it at query time, exactly as before chunking existed.
                out.append((file_path, fallbacks.get(file_path, "")))

        undistilled = [(fp, ev) for fp, ev in out if not by_file[fp]]
        if not undistilled:
            return out
        distilled = dict(await self._distill_snippets(undistilled, project_root))
        return [(fp, distilled.get(fp, ev) if not by_file[fp] else ev) for fp, ev in out]

    @staticmethod
    def _pack_chunk_evidence(hits: List[Tuple[float, str]]) -> str:
        """Greedily pack nearest-first chunk texts under the per-file char budget.

        An unbounded join here would be a token leak: several hits on one file, each
        up to _CHUNK_TEXT_MAX_CHARS, multiplied across _TOP_K files, would dwarf the
        prompt's context budget. Nearest hits are admitted first so the strongest
        evidence always survives and only the weak tail is discarded.
        """
        packed: List[str] = []
        used = 0
        for _, text in sorted(hits, key=lambda h: h[0]):
            cost = len(text)
            if packed and used + cost > _MAX_EVIDENCE_CHARS_PER_FILE:
                break
            packed.append(text[:_MAX_EVIDENCE_CHARS_PER_FILE])
            used += cost
            if used >= _MAX_EVIDENCE_CHARS_PER_FILE:
                break
        return "\n\n".join(packed)

    # ── Query-time evidence distillation (fallback tier) ────────────────
    #
    # A structural note for the next reader: this upgrades evidence quality at
    # *query* time by re-parsing the matched file, costing O(K) parses per call.
    # It is now the FALLBACK tier, not the primary one — files above
    # _CHUNK_FILE_MIN_TOKENS carry stored per-symbol evidence computed once at
    # index time, and _merge_evidence routes those straight through without
    # touching this path. What remains here are files under the chunking
    # threshold, whose ASTs are small by definition, so the residual cost is
    # bounded by construction rather than by the guards below.
    #
    # This cannot be deleted in favour of stored chunks: the store is hybrid by
    # size, so under-threshold files have no chunk rows at all, and removing this
    # would drop them back to the raw content_snippet — a fixed head-of-file slice
    # that is an audit value, not retrieval evidence. If a future change makes
    # every file chunked, this tier can go; until then it is load-bearing.
    # A new caller wanting file evidence should go through _merge_evidence, not here.

    _DISTILL_MAX_CHARS: int = 300_000   # defense-in-depth; see _distill_one
    _DISTILL_TIMEOUT_S: float = 2.0     # generous for tree-sitter; bounds the await only

    async def _distill_snippets(
        self, pairs: List[Tuple[str, str]], project_root: Optional[str],
    ) -> List[Tuple[str, str]]:
        """Best-effort upgrade of each stored content_snippet to a file skeleton.

        Never raises and never returns fewer/reordered pairs than it received —
        a per-file failure falls back to that file's original content_snippet, so
        this can only make evidence better, never break retrieval.
        """
        if not pairs:
            return pairs
        from core.vfs_middleware import make_safe_reader
        reader = make_safe_reader(None, project_root, None)
        out: List[Tuple[str, str]] = []
        for file_path, snippet in pairs:
            out.append((file_path, await self._distill_one(file_path, snippet, reader)))
        return out

    async def _distill_one(
        self,
        file_path: str,
        fallback_snippet: str,
        reader: Callable[[str], Optional[str]],
    ) -> str:
        from shared.contracts import detect_language
        lang = detect_language(file_path)
        if not lang:
            return fallback_snippet
        content = reader(file_path)
        if not content:
            return fallback_snippet
        # Defense-in-depth only, not the primary guard: core/vfs_middleware.py's
        # read_safe() firewall already rejects anything > 500 KB (Layer 3a) and any
        # file containing a line > 1000 chars (Layer 3b, the minification signal)
        # before content ever reaches here — a pathological minified bundle cannot
        # arrive at this point. This second, independent gate exists because that
        # firewall is tuned for "is this worth showing an LLM", not "is this safe
        # to hand a parser"; charter zero-trust-input stance says never rely
        # solely on an upstream filter built for a different purpose.
        if len(content) > self._DISTILL_MAX_CHARS:
            return fallback_snippet
        try:
            from core.ast_engine import extract_skeleton
            skeleton = await asyncio.wait_for(
                asyncio.to_thread(extract_skeleton, content, lang),
                timeout=self._DISTILL_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 — timeout, parse failure, or thread error:
            # never worse than the pre-fix fallback. NOTE: wait_for's timeout frees
            # this coroutine but cannot kill the underlying thread — the shared
            # asyncio default ThreadPoolExecutor keeps running the parse to
            # completion regardless. The _DISTILL_MAX_CHARS guard above is what
            # actually protects that shared pool from a pathological input; this
            # timeout only bounds how long the *caller* waits.
            return fallback_snippet
        return skeleton or fallback_snippet

    # ── Chunk backfill (adoption path for an already-indexed corpus) ───

    async def backfill_chunks(
        self,
        workspace_hash: str,
        project_root: str,
        limit: int = 50,
        force: bool = False,
    ) -> Dict[str, int]:
        """Emit chunk rows for already-embedded files that never got them.

        The indexer only writes chunks as it embeds, and it skips its crawl once a
        workspace is already indexed — so without this, chunking would only ever
        reach files edited after it shipped, permanently excluding the large
        legacy files it exists to fix. Purely additive: reads the file table to
        pick candidates and writes only chunk rows, never touching file-level
        vectors or the dependency graph.

        Bounded and resumable — processes at most ``limit`` files per call and
        reports ``remaining`` so a caller can drive it to completion without an
        unbounded request. Idempotent: files that already have chunk rows are
        skipped unless ``force``.
        """
        empty = {"processed": 0, "chunked": 0, "skipped": 0, "remaining": 0}
        if not workspace_hash or not _SAFE_ID_RE.match(workspace_hash):
            logger.warning(
                "SemanticMemory.backfill_chunks: workspace_hash %r failed sanitization.",
                workspace_hash,
            )
            return empty

        rows = await self.list_embeddings(workspace_hash)
        candidates = [
            str(r.get("file_path", ""))
            for r in rows
            if int(r.get("token_count") or 0) >= _CHUNK_FILE_MIN_TOKENS and r.get("file_path")
        ]
        if not force:
            done = await asyncio.to_thread(self._files_with_chunks, workspace_hash)
            candidates = [p for p in candidates if p not in done]
        if not candidates:
            return empty

        batch, remaining = candidates[:limit], max(0, len(candidates) - limit)
        processed = chunked = skipped = 0
        wrote_any = False

        from core.vfs_middleware import make_safe_reader
        from shared.contracts import IndexingRequest, detect_language
        reader = make_safe_reader(None, project_root, None)

        for file_path in batch:
            processed += 1
            lang = detect_language(file_path)
            if not lang:
                skipped += 1
                continue
            content = reader(file_path)
            if not content:
                skipped += 1
                continue
            try:
                # The same parse the indexer performs, in the same process pool —
                # no bespoke second parser, and tree-sitter stays off the loop.
                from brain.memory import index_file_sync
                from core.compute_pool import compute_pool
                result = await compute_pool.run(
                    index_file_sync,
                    IndexingRequest(
                        file_path=file_path,
                        content=content,
                        language_id=lang,
                        workspace_root=project_root,
                    ),
                )
                if not result.success or not result.symbols:
                    skipped += 1
                    continue
                # Index build is deferred to one pass at the end: rebuilding after
                # every file would repeatedly re-index a growing table.
                written = await self._write_chunks(
                    file_path, content, workspace_hash, result.symbols, build_index=False,
                )
            except Exception as err:  # noqa: BLE001 — one bad file must not abort the pass
                logger.warning(
                    "SemanticMemory.backfill_chunks: %s failed (skipped): %s",
                    file_path, err, exc_info=True,
                )
                skipped += 1
                continue
            if written:
                chunked += 1
                wrote_any = True
            else:
                skipped += 1

        if wrote_any:
            await asyncio.to_thread(self._build_chunk_index_once)

        logger.info(
            "SemanticMemory: chunk backfill — %d processed, %d chunked, %d skipped, %d remaining.",
            processed, chunked, skipped, remaining,
        )
        return {
            "processed": processed,
            "chunked": chunked,
            "skipped": skipped,
            "remaining": remaining,
        }

    def _files_with_chunks(self, workspace_hash: str) -> set[str]:
        """Distinct file paths that already hold chunk rows for this workspace."""
        rows = self._scan_chunk_table(["file_path"], workspace_hash)
        return {str(r["file_path"]) for r in rows if r.get("file_path")}

    def _build_chunk_index_once(self) -> None:
        """Build the chunk ANN index a single time after a backfill pass."""
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _CHUNK_TABLE_NAME not in db.table_names():
            return
        self._build_chunk_index(db.open_table(_CHUNK_TABLE_NAME))

    # ── Vector-map dump (dashboard GraphRAG viewer) ─────

    async def dump_vectors(
        self,
        workspace_hash: str,
        folder_prefix: str = "",
        max_rows: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Read all stored vectors for one workspace (optionally folder-filtered).

        Powers the Memory dashboard /vectors endpoint. Returns a list of
        {file_path, content_snippet, token_count, vector} dicts. Returns [] on
        empty table, sanitization failure, or any error (non-fatal). The blocking
        LanceDB read runs inside asyncio.to_thread.
        """
        if not workspace_hash or not _SAFE_ID_RE.match(workspace_hash):
            logger.warning(
                "SemanticMemory.dump_vectors: workspace_hash %r failed sanitization.",
                workspace_hash,
            )
            return []
        try:
            return await asyncio.to_thread(
                self._dump_vectors_sync, workspace_hash, folder_prefix, max_rows
            )
        except Exception as err:
            logger.warning("SemanticMemory.dump_vectors: failed (non-fatal): %s", err)
            return []

    def _dump_vectors_sync(
        self, workspace_hash: str, folder_prefix: str, max_rows: int
    ) -> List[Dict[str, Any]]:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []
        tbl = db.open_table(_TABLE_NAME)

        cols = ["file_path", "content_snippet", "token_count", "vector"]
        # Predicate pushdown via a PyArrow compute Expression — NOT an SQL string.
        # Lance is version-strict about SQL-string filters; an Expression is both
        # robust and injection-proof (workspace_hash is bound, never interpolated).
        expr = pc.field("workspace_hash") == workspace_hash
        rows: List[Dict[str, Any]]
        try:
            ds = tbl.to_lance()
            try:
                arrow_tbl = ds.to_table(columns=cols, filter=expr)
            except (TypeError, AttributeError):
                # Older/newer Lance: scanner() path with the same Expression.
                arrow_tbl = ds.scanner(columns=cols, filter=expr).to_table()
            rows = arrow_tbl.to_pylist()
        except Exception as primary_err:
            # Last resort: bounded full-table read, then filter in Python. Capped
            # hard so a large multi-project table can never blow up memory.
            logger.debug(
                "dump_vectors: pushdown path unavailable (%s) — bounded fallback.",
                primary_err,
            )
            arrow_tbl = tbl.to_arrow()
            rows = [
                r for r in arrow_tbl.to_pylist()
                if str(r.get("workspace_hash", "")) == workspace_hash
            ]

        if folder_prefix:
            fp = folder_prefix.replace("\\", "/")
            rows = [
                r for r in rows
                if str(r.get("file_path", "")).replace("\\", "/").startswith(fp)
            ]
        return rows[:max_rows]

    # ── Embedding metadata list (dashboard file-embedding browser) ─────

    async def list_embeddings(
        self,
        workspace_hash: str,
        folder_prefix: str = "",
        max_rows: int = 50000,
    ) -> List[Dict[str, Any]]:
        """Read per-file embedding metadata (no vectors) for one workspace.

        Powers the dashboard's file-embedding browser. Returns a list of
        {file_path, content_snippet, token_count, indexed_at} dicts, optionally
        folder-filtered and bounded by max_rows. Excludes the heavy vector column
        (irrelevant to a list). Returns [] on empty table, sanitization failure, or
        any error (non-fatal). The blocking LanceDB read runs inside to_thread.
        """
        if not workspace_hash or not _SAFE_ID_RE.match(workspace_hash):
            logger.warning(
                "SemanticMemory.list_embeddings: workspace_hash %r failed sanitization.",
                workspace_hash,
            )
            return []
        try:
            return await asyncio.to_thread(
                self._list_embeddings_sync, workspace_hash, folder_prefix, max_rows
            )
        except Exception as err:
            logger.warning("SemanticMemory.list_embeddings: failed (non-fatal): %s", err)
            return []

    def _list_embeddings_sync(
        self, workspace_hash: str, folder_prefix: str, max_rows: int
    ) -> List[Dict[str, Any]]:
        import lancedb  # deferred — keep module import light (~1s: lancedb + its namespace REST client)

        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []
        tbl = db.open_table(_TABLE_NAME)

        cols = ["file_path", "content_snippet", "token_count", "indexed_at"]
        # Same injection-proof Expression pushdown as _dump_vectors_sync: the
        # workspace_hash is bound into a PyArrow compute Expression, never an SQL
        # string, so a hostile id can neither widen the scan nor inject a predicate.
        expr = pc.field("workspace_hash") == workspace_hash
        rows: List[Dict[str, Any]]
        try:
            ds = tbl.to_lance()
            try:
                arrow_tbl = ds.to_table(columns=cols, filter=expr)
            except (TypeError, AttributeError):
                arrow_tbl = ds.scanner(columns=cols, filter=expr).to_table()
            rows = arrow_tbl.to_pylist()
        except Exception as primary_err:
            logger.debug(
                "list_embeddings: pushdown path unavailable (%s) — bounded fallback.",
                primary_err,
            )
            arrow_tbl = tbl.to_arrow()
            rows = [
                r for r in arrow_tbl.to_pylist()
                if str(r.get("workspace_hash", "")) == workspace_hash
            ]

        if folder_prefix:
            fp = folder_prefix.replace("\\", "/")
            rows = [
                r for r in rows
                if str(r.get("file_path", "")).replace("\\", "/").startswith(fp)
            ]
        return rows[:max_rows]


# ── Module-level helpers ───────────────────────────────────────────────────


def pca_project_2d(vectors: List[List[float]]) -> Tuple[List[List[float]], List[float], bool]:
    """Project N high-dim vectors to 2D via numpy SVD (PCA). Pure, deterministic.

    Returns (coords, variance_explained, degenerate):
      - coords: list of [x, y] normalized per-axis to [-1, 1].
      - variance_explained: [pc1_frac, pc2_frac] of total variance.
      - degenerate: True when <3 points or the data has no separable variance.

    Determinism: SVD component signs are arbitrary, so each axis is sign-flipped
    to make its largest-magnitude entry positive — this keeps the layout stable
    (no mirror-flip) across repeated requests. No external deps beyond numpy.
    """
    n = len(vectors)
    if n == 0:
        return [], [0.0, 0.0], True
    if n < 3:
        # Too few points to project meaningfully — lay out on a deterministic line.
        coords = [[float(i), 0.0] for i in range(n)]
        return coords, [0.0, 0.0], True

    mat = np.asarray(vectors, dtype=np.float64)
    mean = mat.mean(axis=0)
    centered = mat - mean  # PCA requires mean-centering
    # Economy SVD: centered = U S Vt; principal axes are the rows of Vt.
    _u, s, vt = np.linalg.svd(centered, full_matrices=False)
    comps = vt[:2]                       # (2, dim)
    scores = centered @ comps.T          # (n, 2) projection

    # Deterministic sign per component.
    for j in range(scores.shape[1]):
        col = scores[:, j]
        k = int(np.argmax(np.abs(col)))
        if col[k] < 0:
            scores[:, j] = -col

    total_var = float((s ** 2).sum())
    if total_var > 0:
        var_exp = [float((s[0] ** 2) / total_var), float((s[1] ** 2) / total_var)]
    else:
        var_exp = [0.0, 0.0]

    # Normalize each axis to [-1, 1] (guard a zero-range/degenerate axis).
    for j in range(scores.shape[1]):
        col = scores[:, j]
        lo, hi = float(col.min()), float(col.max())
        rng = hi - lo
        scores[:, j] = (2.0 * (col - lo) / rng - 1.0) if rng > 1e-12 else 0.0

    degenerate = bool(var_exp[0] + var_exp[1] < 1e-9)
    return scores.tolist(), var_exp, degenerate


async def _get_embedding(text: str) -> List[float]:
    """Embed text via the active provider-agnostic target. Async — non-blocking.

    Routing is resolved per the active BYOM preset (Ollama / LM Studio / vLLM /
    OpenAI / custom / legacy proxy). api_base + api_key are applied only when the
    resolved target provides them, so the same call path serves every provider.
    """
    import litellm  # deferred — keep module import light

    t = get_embedding_target()
    kwargs: Dict[str, Any] = {"model": t.model, "input": [text]}
    if t.api_base:
        kwargs["api_base"] = t.api_base
    if t.api_key:
        kwargs["api_key"] = t.api_key
    resp = await litellm.aembedding(**kwargs)
    data: Any = resp.data[0]
    embedding: List[float] = (
        data["embedding"] if isinstance(data, dict) else data.embedding
    )
    return embedding


def _vector_of(datum: Any) -> List[float]:
    """Pull the embedding list out of one response datum (dict or attr shaped)."""
    vec: List[float] = (
        datum["embedding"] if isinstance(datum, dict) else datum.embedding
    )
    return vec


def _index_of(datum: Any, fallback: int) -> int:
    """The datum's declared position in the batch, or ``fallback`` when absent.

    OpenAI-compatible responses carry an ``index`` per datum. Providers are not
    obliged to return them in request order, so the declared index — not list
    position — is what maps a vector back to its input.
    """
    raw = datum.get("index") if isinstance(datum, dict) else getattr(datum, "index", None)
    try:
        return int(raw) if raw is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _partition_for_embedding(texts: Sequence[str], token_budget: int) -> List[List[int]]:
    """Group text indices into request batches.

    A batch closes on whichever bound binds first: ``_EMBED_BATCH_SIZE`` items or
    ``token_budget`` cumulative tokens. The count bound alone is insufficient —
    a handful of large functions can breach a provider's payload ceiling while
    still being well under the item count, which surfaces as HTTP 413 or, worse,
    a silently truncated array. A single text that exceeds the budget on its own
    still gets its own batch rather than being dropped.
    """
    batches: List[List[int]] = []
    current: List[int] = []
    current_tokens = 0
    for i, text in enumerate(texts):
        cost = len(_ENC.encode(text))
        over_count = len(current) >= _EMBED_BATCH_SIZE
        over_tokens = current and (current_tokens + cost) > token_budget
        if over_count or over_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(i)
        current_tokens += cost
    if current:
        batches.append(current)
    return batches


async def _embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed one batch in a single request, ordered to match ``texts``.

    Falls back to sequential single-text calls when the provider returns a
    different number of vectors than were requested: some OpenAI-compatible local
    servers honor only the first element of a batch ``input``, and silently
    accepting a short array would misalign every vector with its symbol.
    """
    import litellm  # deferred — keep module import light

    t = get_embedding_target()
    kwargs: Dict[str, Any] = {"model": t.model, "input": list(texts)}
    if t.api_base:
        kwargs["api_base"] = t.api_base
    if t.api_key:
        kwargs["api_key"] = t.api_key

    resp = await litellm.aembedding(**kwargs)
    data: List[Any] = list(resp.data)
    if len(data) != len(texts):
        logger.warning(
            "SemanticMemory: provider returned %d vectors for a %d-text batch — "
            "falling back to sequential embedding.",
            len(data), len(texts),
        )
        return [await _get_embedding(text) for text in texts]

    ordered: List[List[float]] = [[] for _ in texts]
    for pos, datum in enumerate(data):
        idx = _index_of(datum, pos)
        if not 0 <= idx < len(texts):
            idx = pos
        ordered[idx] = _vector_of(datum)
    return ordered


async def _get_embeddings(texts: Sequence[str]) -> List[List[float]]:
    """Embed many texts, returning vectors strictly in input order.

    Partitioned by item count and token payload, then dispatched concurrently
    under a bounded semaphore so a local single-process provider is not thrashed.
    """
    if not texts:
        return []

    token_budget = get_embedding_target().max_input_tokens
    batches = _partition_for_embedding(texts, token_budget)
    semaphore = asyncio.Semaphore(_EMBED_CONCURRENCY)

    async def _run(indices: List[int]) -> List[List[float]]:
        async with semaphore:
            return await _embed_batch([texts[i] for i in indices])

    results = await asyncio.gather(*(_run(b) for b in batches))

    out: List[List[float]] = [[] for _ in texts]
    for indices, vectors in zip(batches, results):
        for idx, vector in zip(indices, vectors):
            out[idx] = vector
    return out
