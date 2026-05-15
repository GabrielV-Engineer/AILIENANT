# core/memory/graphrag_extractor.py
"""Phase 3.0 — GraphRAG Dynamic Context Extractor.

Performs async k-hop BFS over dependency_graph (SQLite/aiosqlite) and ranks
discovered neighbours by PPR score. Applies token-ceiling and file-count
guardrails (path tokens only — no blocking file I/O) before returning.

Phase 3.1 will add LanceDB semantic scoring; this module provides the
graph-centrality component only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

import aiosqlite
import tiktoken

from shared.config import DB_CATALOG_PATH

logger = logging.getLogger("GRAPHRAG_EXTRACTOR")

# ── Routing-tier constants ─────────────────────────────────────────────────

_K_HOP: Dict[str, int] = {
    "CLOUD":       3,
    "LOCAL_BIG":   1,
    "LOCAL_SMALL": 1,
}

# Token ceilings count path tokens (proxy for prompt overhead).
# Phase 3.1 will replace with actual file-content token counts via VFS.
_TOKEN_CEILING: Dict[str, int] = {
    "LOCAL_SMALL": 4_096,
    "LOCAL_BIG":   16_384,
    "CLOUD":       32_768,
}

_MAX_FILES: Dict[str, int] = {
    "LOCAL_SMALL": 10,
    "LOCAL_BIG":   20,
    "CLOUD":       50,
}

_DEFAULT_ROUTING: str = "LOCAL_SMALL"

# SQLite SQLITE_LIMIT_VARIABLE_NUMBER default is 999; batch to stay safe.
_SQL_CHUNK_SIZE: int = 500

# Module-level singleton — loaded once at worker startup (reading the BPE file
# from disk is a one-time cost). Never instantiate inside __init__ or a hot path.
_ENC: tiktoken.Encoding = tiktoken.get_encoding("cl100k_base")


# ── Public contract ────────────────────────────────────────────────────────


@dataclass
class ExtractionResult:
    """Immutable output of GraphRAGDynamicExtractor.extract()."""

    seed_file: str
    k_hops: int
    neighbors: List[str]           # ranked by PPR descending, post-guardrail
    ppr_scores: Dict[str, float]   # PPR score per kept neighbour
    truncated: bool                # True when guardrail cut the list short
    token_count: int               # sum of path tokens across kept neighbours
    coverage_ratio: float          # len(neighbors) / max_files for this tier (0.0–1.0)


# ── Extractor ─────────────────────────────────────────────────────────────


class GraphRAGDynamicExtractor:
    """Async GraphRAG context extractor backed by the Phase 2.4 SQLite graph.

    Instances are stateless — safe to share across concurrent LangGraph
    fan-out invocations for the same project.

    k-hop depth is driven by routing_decision:
        CLOUD        → k=3  (deep context, 200k-token windows)
        LOCAL_BIG    → k=1  (direct deps only, protect VRAM)
        LOCAL_SMALL  → k=1  (direct deps only, protect VRAM)

    Token guardrail counts path tokens (non-blocking) and enforces a per-tier
    ceiling plus a file-count cap. Phase 3.1 will upgrade to content tokens.
    """

    def __init__(self, project_id: str = "") -> None:
        self._project_id: str = project_id
        # No tiktoken init here — _ENC is the module-level singleton.

    # ── Public entry point ─────────────────────────────────────────────

    async def extract(
        self,
        seed_file: str,
        routing_decision: str,
    ) -> ExtractionResult:
        """Run k-hop BFS from seed_file, rank by PPR, apply guardrails.

        Args:
            seed_file:        File path used as the BFS origin.
            routing_decision: One of "LOCAL_SMALL", "LOCAL_BIG", "CLOUD".
        """
        tier: str = routing_decision if routing_decision in _K_HOP else _DEFAULT_ROUTING
        k: int = _K_HOP[tier]
        ceiling: int = _TOKEN_CEILING[tier]
        max_files: int = _MAX_FILES[tier]

        logger.debug(
            "GraphRAG extract: seed=%s tier=%s k=%d ceiling=%d max_files=%d",
            seed_file, tier, k, ceiling, max_files,
        )

        raw_neighbours: List[str] = await self._bfs_k_hop(seed_file, k)

        if not raw_neighbours:
            logger.debug("GraphRAG: no neighbours found for seed=%s", seed_file)
            return ExtractionResult(
                seed_file=seed_file,
                k_hops=k,
                neighbors=[],
                ppr_scores={},
                truncated=False,
                token_count=0,
                coverage_ratio=0.0,
            )

        ppr_map: Dict[str, float] = await self._fetch_ppr_scores(raw_neighbours)

        ranked: List[str] = sorted(
            raw_neighbours, key=lambda f: ppr_map.get(f, 0.0), reverse=True
        )

        kept, total_tokens, truncated = self._apply_guardrails(ranked, ceiling, max_files)

        # coverage_ratio is tier-relative: kept/max_files so LOCAL_SMALL (10 files)
        # and CLOUD (50 files) both score 1.0 at their respective ceilings.
        coverage_ratio: float = min(1.0, len(kept) / max_files) if max_files > 0 else 0.0

        logger.info(
            "GraphRAG: seed=%s k=%d raw=%d kept=%d tokens=%d truncated=%s coverage=%.3f",
            seed_file, k, len(raw_neighbours), len(kept), total_tokens, truncated, coverage_ratio,
        )

        return ExtractionResult(
            seed_file=seed_file,
            k_hops=k,
            neighbors=kept,
            ppr_scores={f: ppr_map.get(f, 0.0) for f in kept},
            truncated=truncated,
            token_count=total_tokens,
            coverage_ratio=coverage_ratio,
        )

    # ── Private helpers ────────────────────────────────────────────────

    async def _bfs_k_hop(self, seed: str, k: int) -> List[str]:
        """Async BFS over dependency_graph up to k hops from seed.

        Opens a fresh aiosqlite connection per hop level (not per node)
        to batch the entire frontier into a single IN-clause query.
        This is O(k) DB round-trips regardless of graph width.
        The idx_dg_source index on (source_file, project_id) covers these queries.
        """
        visited: set[str] = {seed}
        frontier: List[str] = [seed]
        result: List[str] = []

        for hop in range(k):
            if not frontier:
                break

            next_frontier: List[str] = []

            # Chunk the frontier to stay within SQLITE_LIMIT_VARIABLE_NUMBER.
            for chunk_start in range(0, len(frontier), _SQL_CHUNK_SIZE):
                chunk: List[str] = frontier[chunk_start : chunk_start + _SQL_CHUNK_SIZE]
                placeholders: str = ",".join("?" * len(chunk))
                query: str = (
                    f"SELECT DISTINCT target_dependency "
                    f"FROM dependency_graph "
                    f"WHERE source_file IN ({placeholders}) "
                    f"AND project_id = ?"
                )
                params: Tuple[object, ...] = (*chunk, self._project_id)

                async with aiosqlite.connect(DB_CATALOG_PATH) as db:
                    async with db.execute(query, params) as cur:
                        rows = await cur.fetchall()

                for (target,) in rows:
                    if target and target not in visited:
                        visited.add(target)
                        next_frontier.append(target)
                        result.append(target)

            logger.debug("GraphRAG BFS hop %d/%d: +%d nodes", hop + 1, k, len(next_frontier))
            frontier = next_frontier

        return result

    async def _fetch_ppr_scores(self, files: List[str]) -> Dict[str, float]:
        """Bulk-fetch PPR scores in a single IN-clause query.

        Uses _SQL_CHUNK_SIZE batching for large neighbour sets.
        Files absent from ppr_scores default to 0.0.
        """
        if not files:
            return {}

        scores: Dict[str, float] = {f: 0.0 for f in files}

        for chunk_start in range(0, len(files), _SQL_CHUNK_SIZE):
            chunk = files[chunk_start : chunk_start + _SQL_CHUNK_SIZE]
            placeholders = ",".join("?" * len(chunk))
            query = (
                f"SELECT file_path, ppr_score "
                f"FROM ppr_scores "
                f"WHERE file_path IN ({placeholders}) "
                f"AND project_id = ?"
            )
            params = (*chunk, self._project_id)
            async with aiosqlite.connect(DB_CATALOG_PATH) as db:
                async with db.execute(query, params) as cur:
                    rows = await cur.fetchall()
            for file_path, ppr_score in rows:
                scores[str(file_path)] = float(ppr_score)

        return scores

    def _apply_guardrails(
        self,
        ranked: List[str],
        ceiling: int,
        max_files: int,
    ) -> Tuple[List[str], int, bool]:
        """Apply file-count cap then token ceiling.

        Tokens are counted from file PATH strings (pure CPU via tiktoken,
        zero I/O, non-blocking). This is a proxy for prompt overhead; Phase 3.1
        will replace with actual file-content token counts via VFSMiddleware.

        Returns:
            (kept, total_tokens, truncated)
        """
        kept: List[str] = []
        total: int = 0

        for path in ranked:
            if len(kept) >= max_files:
                return kept, total, True
            path_tokens: int = len(_ENC.encode(path))
            if total + path_tokens > ceiling:
                return kept, total, True
            kept.append(path)
            total += path_tokens

        truncated: bool = len(ranked) > len(kept)
        return kept, total, truncated
