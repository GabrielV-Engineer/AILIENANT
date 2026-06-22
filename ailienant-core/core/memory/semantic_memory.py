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
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import lancedb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import tiktoken

import litellm

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
_MAX_EMBED_TOKENS: int = 8191  # ada-002 context limit

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
    ) -> bool:
        """Embed a file and upsert into workspace_embeddings.

        No-op if content has fewer than _MIN_TOKENS tokens (anti-fragmentation).
        Truncates to _MAX_EMBED_TOKENS tokens via a tiktoken round-trip to avoid
        splitting multibyte characters (never slices raw UTF-8 bytes).

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

        safe_content: str = (
            _ENC.decode(tokens_enc[:_MAX_EMBED_TOKENS]) if token_count > _MAX_EMBED_TOKENS else content
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
            return True
        except Exception as write_err:
            logger.warning("SemanticMemory: write failed (non-fatal): %s", write_err)
            return False

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
        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return
        tbl = db.open_table(_TABLE_NAME)
        safe_path = file_path.replace("'", "''")  # standard SQL single-quote escape
        tbl.delete(f"workspace_hash = '{workspace_hash}' AND file_path = '{safe_path}'")
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
                    "SemanticMemory: embedding dim changed %d → %d — recreating table.",
                    existing_dim, vec_dim,
                )
                db.drop_table(_TABLE_NAME)
                tbl = db.create_table(_TABLE_NAME, schema=schema)
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
        time signal without a second query. Returns (0.0, [], []) on empty input
        or any failure.
        """
        if not user_input.strip():
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

    # ── Phase 7.9.B.15: snippet retrieval for live-chat GraphRAG injection ─────

    def _query_snippets(
        self, vector: List[float], workspace_hash: str, k: int
    ) -> List[Tuple[str, str]]:
        """Return (file_path, content_snippet) pairs for the top-k nearest vectors."""
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

    async def search_snippets(
        self,
        user_input: str,
        workspace_hash: str = "",
        k: int = _TOP_K,
    ) -> List[Tuple[str, str]]:
        """Return (file_path, content_snippet) pairs most relevant to user_input.

        Powers invisible GraphRAG context injection into the live chat system
        prompt. Returns [] on empty input or any failure (non-fatal).
        """
        if not user_input.strip():
            return []

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:  # noqa: BLE001 — RAG must never break a chat turn
            logger.warning("SemanticMemory.search_snippets: embed failed (non-fatal): %s", embed_err)
            return []

        try:
            return await asyncio.to_thread(
                self._query_snippets, vector, workspace_hash, k
            )
        except Exception as query_err:  # noqa: BLE001
            logger.warning("SemanticMemory.search_snippets: query failed (non-fatal): %s", query_err)
            return []

    # ── Phase 7.9.B.1: Vector-map dump (dashboard GraphRAG viewer) ─────

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
