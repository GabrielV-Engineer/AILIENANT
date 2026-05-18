# core/memory/graphrag_extractor.py
"""Phase 3.0 — GraphRAG Dynamic Context Extractor.

Performs async k-hop BFS over dependency_graph (SQLite/aiosqlite) and ranks
discovered neighbours by PPR score. Applies token-ceiling and file-count
guardrails (path tokens only — no blocking file I/O) before returning.

Phase 3.1 will add LanceDB semantic scoring; this module provides the
graph-centrality component only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

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


@dataclass
class DeepParseResult:
    """Output of GraphRAGDynamicExtractor.deep_parse() (Phase 3.2)."""

    target_files: List[str]   # seed + 1-degree neighbors attempted
    parsed_files: List[str]   # files successfully VFS-read + Tree-sitter parsed
    context_block: str        # formatted context string ready for LLM injection
    coverage_ratio: float     # len(parsed_files) / len(target_files); 0.0 if empty
    token_count: int          # tiktoken token count of context_block


# ── Module-level helpers ──────────────────────────────────────────────────


def _extract_top_level_symbols(tree: Any) -> List[str]:
    """Walk top-level Tree-sitter nodes and return named definition identifiers.

    Language-agnostic: checks common node types across Python, TypeScript,
    JavaScript, Java, C#, etc. Returns [] for None trees or unsupported grammars.
    """
    if tree is None:
        return []
    symbols: List[str] = []
    for node in tree.root_node.children:
        name_node: Any = None
        if node.type in (
            "function_definition", "class_definition",
            "function_declaration", "class_declaration",
            "method_definition",
        ):
            name_node = node.child_by_field_name("name")
        elif node.type == "decorated_definition":
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    name_node = child.child_by_field_name("name")
                    break
        elif node.type == "export_statement":
            for child in node.children:
                if child.type in ("function_declaration", "class_declaration"):
                    name_node = child.child_by_field_name("name")
                    break
        if name_node is not None and name_node.text:
            try:
                symbols.append(name_node.text.decode("utf-8", errors="replace"))
            except Exception:
                pass
    return symbols


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

    # ── Phase 5.3 — Public BFS wrappers (forward + backward) ──────────────

    async def bfs_k_hop_forward(self, seed: str, k: int) -> List[str]:
        """Public wrapper: files transitively imported by `seed` up to k hops."""
        return await self._bfs_k_hop(seed, k)

    async def bfs_k_hop_backward(self, seed: str, k: int) -> List[str]:
        """Public wrapper: files that transitively import `seed` (k-hop reverse).

        Uses the symmetric SQL query (source_file ↔ target_dependency swap)
        with the same chunked-IN pattern as the forward walk. Powers
        TraceDataFlowTool's "who could be affected by changing X" view.
        """
        visited: set[str] = {seed}
        frontier: List[str] = [seed]
        result: List[str] = []

        for hop in range(k):
            if not frontier:
                break

            next_frontier: List[str] = []

            for chunk_start in range(0, len(frontier), _SQL_CHUNK_SIZE):
                chunk: List[str] = frontier[chunk_start : chunk_start + _SQL_CHUNK_SIZE]
                placeholders: str = ",".join("?" * len(chunk))
                query: str = (
                    f"SELECT DISTINCT source_file "
                    f"FROM dependency_graph "
                    f"WHERE target_dependency IN ({placeholders}) "
                    f"AND project_id = ?"
                )
                params: Tuple[object, ...] = (*chunk, self._project_id)

                async with aiosqlite.connect(DB_CATALOG_PATH) as db:
                    async with db.execute(query, params) as cur:
                        rows = await cur.fetchall()

                for (source,) in rows:
                    if source and source not in visited:
                        visited.add(source)
                        next_frontier.append(source)
                        result.append(source)

            logger.debug(
                "GraphRAG reverse BFS hop %d/%d: +%d nodes", hop + 1, k, len(next_frontier)
            )
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

    # ── Phase 3.2: Semantic-guided deep parse ─────────────────────────

    async def _expand_neighbors(self, seed_files: List[str]) -> List[str]:
        """Return 1-degree SQLite neighbors for all seed_files in a single batch.

        Reuses the chunked IN-clause pattern from _bfs_k_hop (O(1) DB round-trips
        regardless of seed count, within SQLITE_LIMIT_VARIABLE_NUMBER).
        """
        if not seed_files:
            return []
        visited: set[str] = set(seed_files)
        result: List[str] = []
        for chunk_start in range(0, len(seed_files), _SQL_CHUNK_SIZE):
            chunk: List[str] = seed_files[chunk_start: chunk_start + _SQL_CHUNK_SIZE]
            placeholders: str = ",".join("?" * len(chunk))
            query: str = (
                f"SELECT DISTINCT target_dependency FROM dependency_graph "
                f"WHERE source_file IN ({placeholders}) AND project_id = ?"
            )
            params: Tuple[object, ...] = (*chunk, self._project_id)
            async with aiosqlite.connect(DB_CATALOG_PATH) as db:
                async with db.execute(query, params) as cur:
                    rows = await cur.fetchall()
            for (target,) in rows:
                if target and target not in visited:
                    visited.add(target)
                    result.append(target)
        return result

    async def deep_parse(
        self,
        seed_files: List[str],
        workspace_root: str,
    ) -> DeepParseResult:
        """Expand seed_files 1-degree via SQLite, then VFS-read + Tree-sitter parse.

        Neighbor expansion is async (SQLite). VFS reads and Tree-sitter CPU work
        are wrapped in asyncio.to_thread to avoid blocking the LangGraph event loop.
        """
        if not seed_files:
            return DeepParseResult(
                target_files=[],
                parsed_files=[],
                context_block="",
                coverage_ratio=0.0,
                token_count=0,
            )
        neighbors = await self._expand_neighbors(seed_files)
        # Preserve seed order, append neighbors, deduplicate.
        target_files: List[str] = list(dict.fromkeys([*seed_files, *neighbors]))
        return await asyncio.to_thread(self._deep_parse_sync, target_files, workspace_root)

    def _deep_parse_sync(
        self,
        target_files: List[str],
        workspace_root: str,
    ) -> DeepParseResult:
        """Blocking: VFS read + Tree-sitter parse for each target file.

        Runs inside asyncio.to_thread. Deferred imports isolate VFS/AST from
        module-level loading (consistent with project SPOF guard pattern).
        """
        from core.vfs_middleware import VFSMiddleware
        from core.ast_engine import ASTEngine
        from shared.contracts import detect_language

        vfs = VFSMiddleware()  # type: ignore[no-untyped-call]
        ast_engine = ASTEngine()
        lines: List[str] = ["## Code Context — Semantic Deep Parse (Phase 3.2):"]
        parsed: List[str] = []

        for file_path in target_files:
            vfs_result = vfs.read_safe(
                file_path,
                project_id=self._project_id,
                project_root=workspace_root,
            )
            if not vfs_result.ok or vfs_result.content is None:
                continue
            lang: Any = detect_language(file_path)
            if not lang:
                continue
            tree: Any = ast_engine.parse(file_path, vfs_result.content, lang)
            symbols = _extract_top_level_symbols(tree)
            parsed.append(file_path)
            sym_str = (
                ", ".join(f"`{s}`" for s in symbols[:20])
                if symbols else "(no top-level symbols)"
            )
            lines.append(f"\n### {file_path}  [{lang}]\nSymbols: {sym_str}")

        context_block = "\n".join(lines) if parsed else ""
        token_count = len(_ENC.encode(context_block)) if context_block else 0
        coverage = len(parsed) / len(target_files) if target_files else 0.0
        return DeepParseResult(
            target_files=target_files,
            parsed_files=parsed,
            context_block=context_block,
            coverage_ratio=min(1.0, coverage),
            token_count=token_count,
        )
