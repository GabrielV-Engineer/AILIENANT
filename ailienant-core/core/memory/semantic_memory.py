# core/memory/semantic_memory.py
"""Phase 3.1 — Vector Memory Engine (LanceDB Multi-tenancy & Semantic Upsert).

Embeds every successfully indexed file into a shared LanceDB table
(workspace_embeddings), isolated per workspace via workspace_hash.

semantic_upsert  — called by LazyIndexer after each file is indexed.
search           — called by PlannerAgent to compute ContextMeter.semantic_similarity.

Blocking LanceDB operations run inside asyncio.to_thread.
Embedding generation uses litellm.aembedding() (already async).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import lancedb  # type: ignore[import-untyped]
import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
import tiktoken

import litellm

from shared.config import LANCEDB_PATH
from core.config.embedding_resolver import get_embedding_target

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

    def __init__(self, lancedb_path: str = LANCEDB_PATH) -> None:
        self._lancedb_path = lancedb_path

    # ── Public API ────────────────────────────────────────────────────

    async def semantic_upsert(
        self,
        file_path: str,
        content: str,
        workspace_hash: str,
    ) -> None:
        """Embed a file and upsert into workspace_embeddings.

        No-op if content has fewer than _MIN_TOKENS tokens (anti-fragmentation).
        Truncates to _MAX_EMBED_TOKENS tokens via a tiktoken round-trip to avoid
        splitting multibyte characters (never slices raw UTF-8 bytes).
        """
        tokens_enc = _ENC.encode(content)
        token_count = len(tokens_enc)
        if token_count < _MIN_TOKENS:
            logger.debug(
                "SemanticMemory: skip %s — only %d tokens (< %d).",
                file_path, token_count, _MIN_TOKENS,
            )
            return

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
            return

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
            return tbl.schema.field("vector").type.list_size
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

        tbl = db.open_table(_TABLE_NAME)
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
    ) -> List[Tuple[str, float]]:
        """Like _query_records but returns (file_path, distance) pairs."""
        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []

        tbl = db.open_table(_TABLE_NAME)
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
            (str(r.get("file_path", "")), float(r.get("_distance", 1.0)))
            for r in rows
            if r.get("file_path")
        ]

    # ── Phase 3.2: combined search (single embedding call) ────────────

    async def search_with_paths(
        self,
        user_input: str,
        workspace_hash: str = "",
        k: int = _TOP_K,
    ) -> Tuple[float, List[str]]:
        """Single embedding call returns (aggregated_score, top_k_file_paths).

        Avoids the double embedding call that separate search() + search_files()
        would require. Returns (0.0, []) on empty input or any failure.
        """
        if not user_input.strip():
            return 0.0, []

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:
            logger.warning(
                "SemanticMemory.search_with_paths: embed failed (non-fatal): %s", embed_err
            )
            return 0.0, []

        try:
            pairs: List[Tuple[str, float]] = await asyncio.to_thread(
                self._query_records_with_paths, vector, workspace_hash, k
            )
        except Exception as query_err:
            logger.warning(
                "SemanticMemory.search_with_paths: query failed (non-fatal): %s", query_err
            )
            return 0.0, []

        if not pairs:
            return 0.0, []

        avg = sum(max(0.0, 1.0 - d) for _, d in pairs) / len(pairs)
        score = min(1.0, max(0.0, avg))
        file_paths = [fp for fp, _ in pairs]
        return score, file_paths

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
