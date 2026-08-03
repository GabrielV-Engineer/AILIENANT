# core/memory/graphrag_extractor.py
"""GraphRAG Dynamic Context Extractor.

Performs async BFS over dependency_graph (SQLite/aiosqlite): bounded k-hop
walks for blast-radius/dead-code callers (``bfs_k_hop_forward`` /
``bfs_k_hop_backward``), and a 1-degree "seed + neighbors" expansion for
``deep_parse``, which VFS-reads and Tree-sitter-parses the expanded set into a
symbol-level context block. Neighbors are ranked by PPR centrality before any
budget cap is applied, so the most structurally important dependencies survive
truncation by construction.
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

# ── Budget constants ────────────────────────────────────────────────────────
#
# deep_parse receives no routing-tier signal from its caller (agents/researcher.py
# calls it with only seed_files + workspace_root), so it always budgets against
# the LOCAL_SMALL tier — the same conservative default the rest of the codebase
# falls back to when no tier is known (agents/planner.py, agents/researcher.py
# both default routing_decision="LOCAL_SMALL"). Ceilings are enforced against
# REAL content tokens of the assembled context block (measured incrementally as
# each file's block is built), not a path-length proxy.
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
class DeepParseResult:
    """Output of GraphRAGDynamicExtractor.deep_parse()."""

    target_files: List[str]   # seed + 1-degree neighbors discovered (PRE-cap; the
                               # coverage_ratio denominator — never shrunk by the cap)
    parsed_files: List[str]   # files actually VFS-read + Tree-sitter parsed (POST-cap)
    context_block: str        # formatted context string ready for LLM injection
    coverage_ratio: float     # len(parsed_files) / len(target_files); 0.0 if empty
    token_count: int          # real token count of context_block (incremental tiktoken sum)
    truncated: bool = False   # True when the file-count or token ceiling cut the run short
                               # (never True merely because a file was unreadable/unsupported)


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
    """Async GraphRAG context extractor backed by the SQLite dependency graph.

    Instances are stateless — safe to share across concurrent LangGraph
    fan-out invocations for the same project.

    Two independent surfaces share this class's BFS/PPR primitives:
      - ``bfs_k_hop_forward`` / ``bfs_k_hop_backward``: caller-supplied k-hop
        walks (blast-radius / dead-code analysis), unbounded in scope by design
        — the caller owns the depth and interprets the result.
      - ``deep_parse``: always a 1-degree "seed + neighbors" expansion, ranked
        by PPR and budget-capped (file count + real content tokens) before any
        VFS read or Tree-sitter parse happens, since that I/O+CPU work is what
        actually needs bounding — see deep_parse's own docstring.
    """

    def __init__(self, project_id: str = "") -> None:
        self._project_id: str = project_id
        # No tiktoken init here — _ENC is the module-level singleton.

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

    # ── Public BFS wrappers (forward + backward) ───────────────────────────

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

    # ── Semantic-guided deep parse ──────────────────────────────────────

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

        Neighbors are ranked by PPR centrality before target_files is assembled;
        seeds always come first and keep the caller's own order, since they carry
        actual vector-relevance to the query — PPR is query-blind centrality and
        must never override that signal, only break ties among neighbors that
        have none. The ranked order is what makes the budget cap in
        _deep_parse_sync principled: when it bites, the least-central neighbors
        are what get dropped, not an arbitrary SQL-return-order tail.
        """
        if not seed_files:
            return DeepParseResult(
                target_files=[],
                parsed_files=[],
                context_block="",
                coverage_ratio=0.0,
                token_count=0,
                truncated=False,
            )
        neighbors = await self._expand_neighbors(seed_files)
        ppr_map = await self._fetch_ppr_scores(neighbors)
        ranked_neighbors = sorted(neighbors, key=lambda f: ppr_map.get(f, 0.0), reverse=True)
        # Preserve seed order first, then PPR-ranked neighbors, deduplicate.
        target_files: List[str] = list(dict.fromkeys([*seed_files, *ranked_neighbors]))
        return await asyncio.to_thread(
            self._deep_parse_sync,
            target_files,
            workspace_root,
            _MAX_FILES[_DEFAULT_ROUTING],
            _TOKEN_CEILING[_DEFAULT_ROUTING],
        )

    def _deep_parse_sync(
        self,
        target_files: List[str],
        workspace_root: str,
        max_files: int,
        token_ceiling: int,
    ) -> DeepParseResult:
        """Blocking: VFS read + Tree-sitter parse for each target file, budget-bounded.

        Runs inside asyncio.to_thread. Deferred imports isolate VFS/AST from
        module-level loading (consistent with project SPOF guard pattern).

        Stops once max_files files have been parsed, or once the next file's
        block would push the running context_block past token_ceiling — both
        measured against REAL parsed content (each file's rendered block is
        tiktoken-encoded before being committed), never a path-length proxy.
        Token accounting sums each block's own encoding rather than encoding the
        final joined string once; this is a deliberately conservative choice
        forced by the early-stop logic (a block's cost must be known before
        deciding whether it fits) and may differ marginally from encoding the
        whole string at once, since BPE can merge slightly differently across a
        boundary — never materially, and never in the direction of undercounting.

        coverage_ratio is computed against len(target_files) — the full, PRE-cap
        neighbor set — so a truncated run is visible as reduced coverage (and
        surfaces into the caller's CSS/graph_coverage metric) rather than being
        silently reported as complete.
        """
        from core.vfs_middleware import VFSMiddleware
        from core.ast_engine import ASTEngine
        from shared.contracts import detect_language

        vfs = VFSMiddleware()
        ast_engine = ASTEngine()
        header = "## Code Context — Semantic Deep Parse:"
        lines: List[str] = [header]
        parsed: List[str] = []
        total_tokens: int = len(_ENC.encode(header))
        truncated: bool = False

        for file_path in target_files:
            if len(parsed) >= max_files:
                truncated = True
                break
            vfs_result = vfs.read_safe(
                file_path,
                project_id=self._project_id,
                project_root=workspace_root,
            )
            if not vfs_result.ok or vfs_result.content is None:
                continue  # unreadable/excluded — not a budget truncation
            lang: Any = detect_language(file_path)
            if not lang:
                continue  # unsupported language — not a budget truncation
            tree: Any = ast_engine.parse(file_path, vfs_result.content, lang)
            symbols = _extract_top_level_symbols(tree)
            sym_str = (
                ", ".join(f"`{s}`" for s in symbols[:20])
                if symbols else "(no top-level symbols)"
            )
            block = f"\n### {file_path}  [{lang}]\nSymbols: {sym_str}"
            block_tokens = len(_ENC.encode(block))
            if total_tokens + block_tokens > token_ceiling:
                truncated = True
                break
            lines.append(block)
            total_tokens += block_tokens
            parsed.append(file_path)

        context_block = "\n".join(lines) if parsed else ""
        coverage = len(parsed) / len(target_files) if target_files else 0.0
        return DeepParseResult(
            target_files=target_files,
            parsed_files=parsed,
            context_block=context_block,
            coverage_ratio=min(1.0, coverage),
            token_count=total_tokens if parsed else 0,
            truncated=truncated,
        )
