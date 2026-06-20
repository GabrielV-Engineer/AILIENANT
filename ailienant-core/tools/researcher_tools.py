"""Wave 1 researcher arsenal — five net-new READ_ONLY tools for the Researcher role.

Every tool follows the perception_tools.py convention:
  - async-only (_arun); _run raises NotImplementedError
  - heavy dependencies injected via PrivateAttr + __init__ (never LLM-visible)
  - untrusted content wrapped by tools.quarantine.wrap_boundary
  - bounded output: caps enforced before return; event loop never blocked for I/O

Tools registered here (all READ_ONLY, allowed_roles={"researcher"}):
  glob                — fnmatch path listing over the RAM ∪ indexed-catalog universe
  grep                — regex content search with mandatory O(L) short-circuit
  workspace_structure — relevance-filtered directory tree over the same universe
  query_graphrag      — wraps GraphRAGDynamicExtractor.deep_parse; already token-digested
  get_dependents      — structured-JSON 1-hop backward edge lookup (core.db.get_dependents)

The `read_file` schema (FormalisedReadFileInput) is also registered here to give the
Researcher a retrievable schema for the already-executable read_file @tool produced by
tools.agent_tools.make_read_file_tool.  Schema only; execution is unchanged.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, FrozenSet, Iterable, List, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.quarantine import wrap_boundary

logger = logging.getLogger("RESEARCHER_TOOLS")

# ── Output caps (token hygiene §5.5) ──────────────────────────────────────────
_GLOB_MAX_RESULTS = 200
_GREP_MAX_MATCHES = 50
_GREP_MAX_LINE_CHARS = 200
_WORKSPACE_MAX_NODES = 300

# ── Grep ReDoS bounds ─────────────────────────────────────────────────────────
# A pathological regex on adversarial content can blow up super-linearly. The
# stdlib `re` engine has no per-call timeout and a third-party engine is rejected
# (§9). We bound the damage two ways: cap the bytes any single line presents to
# the matcher, and abort the whole scan past a wall-clock deadline (returning the
# matches found so far rather than hanging the worker thread).
_GREP_SEARCH_INPUT_CAP = 10_000
_GREP_SCAN_DEADLINE_S = 5.0

# Regex metacharacters that terminate a literal run during FTS-literal extraction.
_REGEX_META = set(".^$*+?()[]{}\\|")

# ── Role assignment ────────────────────────────────────────────────────────────
_RESEARCHER_ROLES: FrozenSet[str] = frozenset({"researcher"})
# workspace_structure and get_dependents are shared with the planner role so the
# Planner can inspect repository layout and dependency edges during pre-commit checks.
_RESEARCHER_AND_PLANNER: FrozenSet[str] = _RESEARCHER_ROLES | frozenset({"planner"})


# ── Path canonicalization (audit fix #1) ──────────────────────────────────────
def _canon(path: str) -> str:
    """Normalize a path to a canonical form for safe set-based deduplication.

    Applies normcase (lower-casing + sep normalization on Windows) then normpath
    so that C:\\a\\B.py and c:/a/b.py canonicalize to the same key before the
    RAM ∪ catalog union is built.
    """
    return os.path.normcase(os.path.normpath(path))


# ── Path-provider factory ──────────────────────────────────────────────────────

def make_vfs_path_provider(
    project_id: str,
    *,
    vfs: Any = None,
) -> Callable[[], Awaitable[List[str]]]:
    """Return an async callable that yields RAM-buffer ∪ indexed-catalog paths.

    Both sources are canonicalized via _canon before the set union so that a
    path stored in the VFS as C:\\A\\B.py and in the DB as c:/a/b.py de-duplicate
    to a single entry. Result is sorted for deterministic ordering.

    The provider is async because list_indexed_files hits aiosqlite; construction
    is synchronous (no DB I/O until the first _arun call).
    """
    from core.db import list_indexed_files
    from core.vfs_middleware import VFSMiddleware

    _vfs = vfs if vfs is not None else VFSMiddleware()

    async def _provider() -> List[str]:
        ram_paths = {_canon(p) for p in _vfs.snapshot_paths()}
        db_paths = {_canon(p) for p in await list_indexed_files(project_id)}
        return sorted(ram_paths | db_paths)

    return _provider


# ── FTS narrowing factory (DEBT-041) ─────────────────────────────────────────

def _extract_fts_literal(pattern: str) -> Optional[str]:
    """Lift the longest contiguous literal substring that must appear in every match.

    Used as a trigram pre-filter token. Returns None when no literal of >=3 chars can
    be guaranteed, so the caller full-scans rather than narrow unsafely. Conservative:
    bails on alternation (one literal cannot cover every branch) and ends a run before
    any character made optional by a following ``?``/``*``/``{`` quantifier.
    """
    if "|" in pattern:
        return None
    best = ""
    run: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        nxt = pattern[i + 1] if i + 1 < n else ""
        if ch in _REGEX_META or nxt in ("?", "*", "{"):
            if len(run) > len(best):
                best = "".join(run)
            run = []
            i += 1
            continue
        run.append(ch)
        i += 1
    if len(run) > len(best):
        best = "".join(run)
    return best if len(best) >= 3 else None


def make_fts_narrow_provider(
    project_id: str,
    *,
    vfs: Any = None,
) -> Callable[[str, List[str]], Awaitable[Optional[List[str]]]]:
    """Return an async narrower: ``(literal, paths) -> Optional[superset]``.

    Wraps ``core.db.fts_narrow_catalog`` and force-includes the live RAM buffers:
    a buffer's in-memory bytes can differ from its last-indexed revision, so the
    line index can never authorize excluding it. Returns None to signal the caller
    to full-scan (FTS unavailable or no safe literal). The returned set is always a
    SUPERSET of the true matches — the caller still regex-confirms every candidate.
    """
    from core.db import fts_narrow_catalog
    from core.vfs_middleware import VFSMiddleware

    _vfs = vfs if vfs is not None else VFSMiddleware()

    async def _narrow(literal: str, paths: List[str]) -> Optional[List[str]]:
        narrowed = await fts_narrow_catalog(project_id, literal, paths)
        if narrowed is None:
            return None
        ram = {_canon(p) for p in _vfs.snapshot_paths()}
        keep = set(narrowed)
        keep.update(p for p in paths if _canon(p) in ram)
        return sorted(keep)

    return _narrow


# =====================================================================
# GlobTool
# =====================================================================


class GlobInput(BaseModel):
    pattern: str = Field(description="fnmatch glob pattern, e.g. '**/*.py' or 'src/*.ts'.")
    limit: int = Field(
        default=_GLOB_MAX_RESULTS,
        ge=1,
        le=_GLOB_MAX_RESULTS,
        description="Max paths returned.",
    )


class GlobTool(BaseTool):
    """List workspace files matching a glob pattern (RAM buffers ∪ indexed catalog).

    Never touches disk directly. Pattern is matched via fnmatch against the
    canonicalized path universe. Results are capped by `limit`.
    """

    name: str = "glob"
    description: str = (
        "List workspace files matching an fnmatch glob pattern (e.g. '**/*.py'). "
        "Searches the in-memory VFS buffer set and the indexed file catalog. "
        "No direct disk access. Results capped at 200."
    )
    args_schema: Type[BaseModel] = GlobInput

    _path_provider: Callable[[], Awaitable[List[str]]] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        path_provider: Callable[[], Awaitable[List[str]]],
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._path_provider = path_provider
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GlobTool is async-only — use _arun().")

    async def _arun(self, pattern: str, limit: int = _GLOB_MAX_RESULTS) -> str:
        paths = await self._path_provider()
        matched: List[str] = []
        for p in paths:
            if fnmatch.fnmatch(p, pattern):
                matched.append(p)
                if len(matched) >= limit:
                    break
        if not matched:
            text = f"[glob] No files matched pattern {pattern!r}."
        else:
            text = "\n".join(matched)
            if len(matched) >= limit:
                text += f"\n[glob] Output capped at {limit} results."
        return wrap_boundary(text, self._boundary_provider)


# =====================================================================
# GrepTool
# =====================================================================


class GrepInput(BaseModel):
    pattern: str = Field(description="Python regex pattern to search for in file contents.")
    file_glob: str = Field(
        default="*",
        description="Optional fnmatch glob to pre-filter files (e.g. '*.py').",
    )
    max_matches: int = Field(
        default=_GREP_MAX_MATCHES,
        ge=1,
        le=_GREP_MAX_MATCHES,
        description="Maximum match rows returned.",
    )


class GrepTool(BaseTool):
    """Regex content search over the workspace (VFS RAM-first, indexed-catalog fallback).

    Implements a mandatory O(max_matches) short-circuit: the file loop breaks the
    instant the match counter hits the cap so latency is bounded regardless of
    project size. File I/O is offloaded to asyncio.to_thread to avoid blocking the
    event loop. RAM buffers are read synchronously from the injected content_reader
    (zero I/O); disk-only catalog paths fall back to the firewalled read_safe reader.
    """

    name: str = "grep"
    description: str = (
        "Regex search over workspace file contents. Searches RAM VFS buffers first "
        "(zero I/O), then falls back to the indexed file catalog via the firewalled "
        "reader. Results are capped at 50 matches; each line truncated to 200 chars."
    )
    args_schema: Type[BaseModel] = GrepInput

    _path_provider: Callable[[], Awaitable[List[str]]] = PrivateAttr()
    _content_reader: Callable[[str], Optional[str]] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)
    _narrow_provider: Optional[
        Callable[[str, List[str]], Awaitable[Optional[List[str]]]]
    ] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        path_provider: Callable[[], Awaitable[List[str]]],
        content_reader: Callable[[str], Optional[str]],
        boundary_provider: Optional[Callable[[], str]] = None,
        narrow_provider: Optional[
            Callable[[str, List[str]], Awaitable[Optional[List[str]]]]
        ] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._path_provider = path_provider
        self._content_reader = content_reader
        self._boundary_provider = boundary_provider
        self._narrow_provider = narrow_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GrepTool is async-only — use _arun().")

    async def _arun(
        self,
        pattern: str,
        file_glob: str = "*",
        max_matches: int = _GREP_MAX_MATCHES,
    ) -> str:
        # Defensive regex compile (audit fix #6 — zero-trust input §6.2)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return wrap_boundary(
                f"[grep] ERROR: invalid regex pattern {pattern!r}: {exc}",
                self._boundary_provider,
            )

        paths = await self._path_provider()
        if file_glob != "*":
            paths = [p for p in paths if fnmatch.fnmatch(p, file_glob)]

        # Pre-filter to candidate files via the content index when a safe literal can
        # be lifted from the pattern. The narrower returns a SUPERSET of the true
        # matches (or None to full-scan), so the regex confirm below never misses one.
        if self._narrow_provider is not None:
            literal = _extract_fts_literal(pattern)
            if literal is not None:
                narrowed = await self._narrow_provider(literal, paths)
                if narrowed is not None:
                    paths = narrowed

        rows: List[str] = []

        def _scan(deadline: float) -> List[str]:
            """Run in asyncio.to_thread — all sync I/O stays off the event loop."""
            result: List[str] = []
            for path in paths:
                # ReDoS / runaway guard: abort past the wall-clock deadline and
                # return what was found rather than pinning the worker thread.
                if time.monotonic() > deadline:
                    break
                content = self._content_reader(path)
                if content is None:
                    continue
                for lineno, line in enumerate(content.splitlines(), start=1):
                    # Bound the bytes any single line presents to the matcher.
                    if compiled.search(line[:_GREP_SEARCH_INPUT_CAP]):
                        truncated = line[:_GREP_MAX_LINE_CHARS]
                        result.append(f"{path}:{lineno}:{truncated}")
                        # O(L) short-circuit — break as soon as cap is hit
                        if len(result) >= max_matches:
                            return result
            return result

        rows = await asyncio.to_thread(_scan, time.monotonic() + _GREP_SCAN_DEADLINE_S)

        if not rows:
            text = f"[grep] No matches for pattern {pattern!r}."
        else:
            text = "\n".join(rows)
            if len(rows) >= max_matches:
                text += f"\n[grep] Output capped at {max_matches} matches."
        return wrap_boundary(text, self._boundary_provider)


# =====================================================================
# WorkspaceStructureTool
# =====================================================================


class WorkspaceStructureInput(BaseModel):
    subtree: str = Field(
        default="",
        description="Optional path prefix to restrict the tree (e.g. 'src/core').",
    )
    pattern: str = Field(
        default="*",
        description="Optional fnmatch glob to filter leaf file names (e.g. '*.py').",
    )


class WorkspaceStructureTool(BaseTool):
    """Emit a relevance-filtered workspace directory tree (RAM ∪ indexed catalog).

    Useful for orientation before deep reads. Output is an indented tree capped
    at 300 nodes; no disk access.
    """

    name: str = "workspace_structure"
    description: str = (
        "Show an indented directory tree of the workspace (VFS RAM buffers ∪ "
        "indexed catalog). Filter by subtree prefix or fnmatch pattern. "
        "Capped at 300 nodes. No direct disk access."
    )
    args_schema: Type[BaseModel] = WorkspaceStructureInput

    _path_provider: Callable[[], Awaitable[List[str]]] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        path_provider: Callable[[], Awaitable[List[str]]],
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._path_provider = path_provider
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("WorkspaceStructureTool is async-only — use _arun().")

    async def _arun(self, subtree: str = "", pattern: str = "*") -> str:
        paths = await self._path_provider()

        # Apply subtree prefix filter (canonicalized for consistent matching)
        if subtree:
            canon_sub = _canon(subtree)
            paths = [p for p in paths if p.startswith(canon_sub)]

        # Apply leaf-name pattern filter
        if pattern != "*":
            paths = [p for p in paths if fnmatch.fnmatch(os.path.basename(p), pattern)]

        if not paths:
            return wrap_boundary("[workspace_structure] No matching files.", self._boundary_provider)

        # Build an indented tree from the sorted path list
        lines = _build_tree_lines(paths[:_WORKSPACE_MAX_NODES])
        text = "\n".join(lines)
        if len(paths) > _WORKSPACE_MAX_NODES:
            text += f"\n[workspace_structure] Tree capped at {_WORKSPACE_MAX_NODES} nodes."
        return wrap_boundary(text, self._boundary_provider)


def _build_tree_lines(paths: Iterable[str]) -> List[str]:
    """Convert a flat sorted list of canonical paths into an indented tree."""
    lines: List[str] = []
    prev_parts: List[str] = []
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        # Find common prefix depth
        common = 0
        for a, b in zip(prev_parts, parts):
            if a == b:
                common += 1
            else:
                break
        for depth, part in enumerate(parts[common:], start=common):
            indent = "  " * depth
            lines.append(f"{indent}{part}")
        prev_parts = parts
    return lines


# =====================================================================
# GraphRAGQueryTool
# =====================================================================


class GraphRAGQueryInput(BaseModel):
    seed_files: List[str] = Field(
        description="Workspace-relative paths to use as GraphRAG seed files."
    )


class GraphRAGQueryTool(BaseTool):
    """Expand seed files through the GraphRAG dependency graph and return a context block.

    Wraps GraphRAGDynamicExtractor.deep_parse — 1-hop neighbor expansion via SQLite,
    then VFS read + Tree-sitter parse. The returned context_block is already
    token-digested by deep_parse so it never inflates the context window unboundedly.
    workspace_root is injected at construction time (runtime context, not an LLM arg).
    """

    name: str = "query_graphrag"
    description: str = (
        "Expand seed files through the GraphRAG dependency graph (1-hop neighbor "
        "expansion + VFS read + Tree-sitter parse). Returns a compact context block "
        "with signatures and relationships. Provide workspace-relative file paths."
    )
    args_schema: Type[BaseModel] = GraphRAGQueryInput

    _extractor: Any = PrivateAttr()
    _workspace_root: str = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        extractor: Any,
        workspace_root: str,
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._extractor = extractor
        self._workspace_root = workspace_root
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GraphRAGQueryTool is async-only — use _arun().")

    async def _arun(self, seed_files: List[str]) -> str:
        try:
            result = await self._extractor.deep_parse(
                seed_files=seed_files,
                workspace_root=self._workspace_root,
            )
            text = result.context_block or "[query_graphrag] No context extracted."
        except Exception as exc:  # noqa: BLE001 — surface parse errors gracefully
            logger.warning("GraphRAGQueryTool deep_parse failed: %s", exc, exc_info=True)
            text = f"[query_graphrag] ERROR: {exc}"
        return wrap_boundary(text, self._boundary_provider)


# =====================================================================
# GetDependentsTool
# =====================================================================


class GetDependentsInput(BaseModel):
    target_file_path: str = Field(
        description="Workspace-relative path to look up inbound edges for."
    )


class GetDependentsTool(BaseTool):
    """Return structured JSON listing files that import the given target file (1-hop).

    Wraps core.db.get_dependents via a context-bound callable injected at
    construction time (project_id pre-bound via functools.partial). The output is
    a JSON object — deliberately distinct from GetSymbolReferencesTool's
    refactor-impact prose so both can coexist in the tool catalog.
    """

    name: str = "get_dependents"
    description: str = (
        "Return a JSON list of files that import the given file (1-hop backward "
        "dependency edges). Use for impact analysis before a refactor. "
        "Returns {\"target\": ..., \"dependents\": [...]}."
    )
    args_schema: Type[BaseModel] = GetDependentsInput

    # Context-bound callable: functools.partial(core.db.get_dependents, project_id=...)
    _get_dependents: Callable[..., Awaitable[List[str]]] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        get_dependents: Callable[..., Awaitable[List[str]]],
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._get_dependents = get_dependents
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GetDependentsTool is async-only — use _arun().")

    async def _arun(self, target_file_path: str) -> str:
        try:
            dependents = await self._get_dependents(target_file_path)
        except Exception as exc:  # noqa: BLE001 — DB failures degrade gracefully
            logger.warning("GetDependentsTool failed for %r: %s", target_file_path, exc, exc_info=True)
            dependents = []
        payload = {"target": target_file_path, "dependents": dependents}
        return wrap_boundary(json.dumps(payload, indent=2), self._boundary_provider)


# =====================================================================
# Schema registration helper
# =====================================================================


class FormalisedReadFileInput(BaseModel):
    """Schema formalization for the already-executable read_file @tool.

    Execution flows through tools.agent_tools.make_read_file_tool — this model
    exists solely to give the Researcher a retrievable schema in the ToolRAGStore.
    """

    path: str = Field(description="Workspace-relative path to read.")
    offset: int = Field(default=0, description="Line-based read offset (0 = start).")
    limit: Optional[int] = Field(default=None, description="Max lines to return (None = all).")


def _tool_schema(
    name: str,
    description: str,
    json_schema_class: Type[BaseModel],
    *,
    roles: FrozenSet[str] = _RESEARCHER_ROLES,
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=roles,
    )


async def register_researcher_tools(store: ToolRAGStore) -> int:
    """Register the 6 researcher-scoped schemas in the given store. Returns count.

    Includes 5 net-new tools and 1 schema formalization of the existing read_file
    @tool. None auto-register at module import — callers do this explicitly.
    """
    schemas: List[ToolSchema] = [
        _tool_schema(
            "read_file",
            "Read a workspace file with optional line-based offset/limit pagination.",
            FormalisedReadFileInput,
        ),
        _tool_schema(
            "glob",
            "List workspace files matching an fnmatch glob pattern (RAM ∪ indexed catalog).",
            GlobInput,
        ),
        _tool_schema(
            "grep",
            "Regex search over workspace file contents (RAM-first, firewalled fallback).",
            GrepInput,
        ),
        _tool_schema(
            "workspace_structure",
            "Show an indented directory tree of the workspace (RAM ∪ indexed catalog).",
            WorkspaceStructureInput,
            roles=_RESEARCHER_AND_PLANNER,
        ),
        _tool_schema(
            "query_graphrag",
            "Expand seed files via GraphRAG and return a compact context block.",
            GraphRAGQueryInput,
        ),
        _tool_schema(
            "get_dependents",
            "Return JSON {target, dependents[]} of files that import the given file.",
            GetDependentsInput,
            roles=_RESEARCHER_AND_PLANNER,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
