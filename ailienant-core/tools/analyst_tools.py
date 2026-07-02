"""Wave 2 analyst arsenal — seven net-new READ_ONLY tools for the Analyst role.

Every tool follows the perception_tools.py / researcher_tools.py convention:
  - async-only (_arun); _run raises NotImplementedError
  - heavy dependencies injected via PrivateAttr + __init__ (never LLM-visible)
  - untrusted content wrapped by tools.quarantine.wrap_boundary
  - bounded output: caps enforced before return; event loop never blocked for I/O

Tools registered here (all READ_ONLY, allowed_roles={"analyst"}):
  run_linter          — ruff/eslint diagnostics via tools.validation.lsp_filter.validate_lsp
  analyze_complexity  — McCabe CC + nesting depth via stdlib ast (Python files only)
  audit_dependencies  — manifest parse (requirements.txt / pyproject.toml / package.json)
  diff_changes        — unified diff of dirty RAM buffer vs on-disk original
  web_search          — injectable search callable (brave-search MCP compatible)
  read_token_ledger   — live token-cost snapshot from core.token_ledger.TokenLedger
                        (also surfaced to the orchestrator for budget telemetry)
  detect_dead_code    — zero-resolved-in-degree, non-entrypoint file candidates
                        via core.dead_code.compute_dead_code
  architecture_digest — bounded project overview from persisted graph analytics
                        (defined in perception_tools.py; wired here for the Analyst)

Security: every disk read is confined to workspace_root via _jailed_disk_read (path
traversal check using pathlib.resolve().is_relative_to). LLM-supplied file paths
that escape the workspace jail are silently rejected before any file handle opens.
"""

from __future__ import annotations

import ast
import itertools
import json
import logging
import os
import pathlib
from difflib import unified_diff
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Optional,
    Type,
)

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.token_ledger import TokenLedger
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.quarantine import wrap_boundary

if TYPE_CHECKING:
    from core.tool_dispatch import RegisteredTool

logger = logging.getLogger("ANALYST_TOOLS")

# ── Output caps (token hygiene §5.5) ──────────────────────────────────────────
_DISK_MAX_BYTES: int = 100 * 1024   # 100 KB — shared cap for disk reads + ast.parse guard
_LINT_MAX_ERRORS: int = 50
_DIFF_MAX_LINES: int = 300
_CVE_MAX_DEPS: int = 20

# ── Role assignment ────────────────────────────────────────────────────────────
_ANALYST_ROLES: FrozenSet[str] = frozenset({"analyst"})
# read_token_ledger is shared with the orchestrator (its live token-spend view doubles
# as the orchestrator's budget telemetry — no second tool needed).
_ANALYST_AND_ORCHESTRATOR: FrozenSet[str] = _ANALYST_ROLES | frozenset({"orchestrator"})

# ── Manifest filename allowlist ────────────────────────────────────────────────
_MANIFEST_NAMES: FrozenSet[str] = frozenset(
    {"requirements.txt", "pyproject.toml", "setup.cfg", "package.json"}
)


# =====================================================================
# Workspace-jailed disk reader (security §6.2)
# =====================================================================


def _jailed_disk_read(path: str, workspace_root: str) -> Optional[str]:
    """Read a file from disk, confined to workspace_root.

    Returns None on jail violation, size-limit breach, or any I/O failure.
    Callers decide the semantics of None (e.g. CodeDiffTool treats a missing
    disk file as an empty original to produce an all-additions diff).
    """
    try:
        resolved = pathlib.Path(path).resolve()
        jail = pathlib.Path(workspace_root).resolve()
        if not resolved.is_relative_to(jail):
            logger.warning("_jailed_disk_read: path %s escapes workspace jail %s", path, workspace_root)
            return None
        size = resolved.stat().st_size
        if size > _DISK_MAX_BYTES:
            logger.debug("_jailed_disk_read: %s exceeds %d byte cap (%d bytes)", path, _DISK_MAX_BYTES, size)
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None


# =====================================================================
# RunLinterTool
# =====================================================================


class RunLinterInput(BaseModel):
    file_path: str = Field(description="Workspace-relative or absolute path to lint.")
    timeout: float = Field(default=10.0, ge=1.0, le=60.0, description="Subprocess timeout in seconds.")


class RunLinterTool(BaseTool):
    """Run ruff (Python) or eslint (TS/TSX) on a file and return diagnostics.

    Reads content from the VFS RAM buffer first (dirty/AI-modified version);
    falls back to the on-disk copy via the workspace-jailed disk reader.
    Gracefully degrades when the linter binary is absent — inherits the
    validate_lsp pass-through behaviour (returns is_valid=True with no errors).
    """

    name: str = "run_linter"
    description: str = (
        "Lint a source file with ruff (Python) or eslint (TypeScript/TSX). "
        "Returns a JSON object with is_valid, error count, and up to "
        f"{_LINT_MAX_ERRORS} diagnostics. Degrades gracefully when linters are absent."
    )
    args_schema: Type[BaseModel] = RunLinterInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _workspace_root: str = PrivateAttr()
    _ram_reader: Callable[[str], Optional[str]] = PrivateAttr()

    def __init__(
        self,
        *,
        workspace_root: str,
        ram_reader: Callable[[str], Optional[str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_root = workspace_root
        self._ram_reader = ram_reader

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("RunLinterTool is async-only — use _arun().")

    async def _arun(self, file_path: str, timeout: float = 10.0) -> str:
        from tools.validation.lsp_filter import validate_lsp

        # Cascade read: RAM buffer first, disk fallback
        content = self._ram_reader(file_path)
        if content is None:
            import asyncio
            content = await asyncio.to_thread(_jailed_disk_read, file_path, self._workspace_root)
        if content is None:
            return json.dumps({"error": "file not found", "file_path": file_path})

        # validate_lsp is already async (asyncio.create_subprocess_exec)
        result = await validate_lsp(content, file_path, timeout)

        capped_errors = [
            {"layer": e.layer, "message": e.message, "line": e.line, "column": e.column}
            for e in result.errors[:_LINT_MAX_ERRORS]
        ]
        return json.dumps(
            {
                "file_path": file_path,
                "is_valid": result.is_valid,
                "error_count": len(result.errors),
                "errors": capped_errors,
                "truncated": len(result.errors) > _LINT_MAX_ERRORS,
            }
        )


# =====================================================================
# ComplexityAnalysisTool
# =====================================================================


class ComplexityInput(BaseModel):
    file_path: str = Field(description="Path to a Python (.py) source file to analyse.")


def _compute_cc(tree: ast.AST) -> int:
    """McCabe cyclomatic complexity for an AST module: branch count + 1."""
    cc = 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler, ast.With, ast.Assert)):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            cc += 1
    return cc


def _max_depth(node: ast.AST, current: int = 0) -> int:
    """Recursive nesting depth via child iteration."""
    child_depths = [
        _max_depth(child, current + 1)
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.stmt)
    ]
    return max(child_depths, default=current)


def _per_function_cc(tree: ast.AST) -> List[dict]:  # type: ignore[type-arg]
    """Per-function McCabe CC breakdown."""
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results.append(
                {"name": node.name, "lineno": node.lineno, "cc": _compute_cc(node)}
            )
    return results


class ComplexityAnalysisTool(BaseTool):
    """Compute McCabe cyclomatic complexity and nesting depth for a Python file.

    Returns a JSON object with module-level CC, max nesting depth, and a
    per-function breakdown. Non-Python files receive a line-count summary only.
    Files larger than 100 KB or with deeply pathological ASTs (RecursionError)
    are rejected with a clean error payload — never crash the agent node.
    """

    name: str = "analyze_complexity"
    description: str = (
        "Compute McCabe cyclomatic complexity and nesting depth for a Python file. "
        "Returns module CC, max nesting depth, and per-function CC breakdown as JSON. "
        "Non-.py files receive a line-count summary only."
    )
    args_schema: Type[BaseModel] = ComplexityInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _workspace_root: str = PrivateAttr()
    _ram_reader: Callable[[str], Optional[str]] = PrivateAttr()

    def __init__(
        self,
        *,
        workspace_root: str,
        ram_reader: Callable[[str], Optional[str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_root = workspace_root
        self._ram_reader = ram_reader

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ComplexityAnalysisTool is async-only — use _arun().")

    async def _arun(self, file_path: str) -> str:
        import asyncio

        # Cascade read
        content = self._ram_reader(file_path)
        if content is None:
            content = await asyncio.to_thread(_jailed_disk_read, file_path, self._workspace_root)
        if content is None:
            return json.dumps({"error": "file not found", "file_path": file_path})

        ext = os.path.splitext(file_path)[1].lower()
        if ext != ".py":
            return json.dumps(
                {
                    "file": file_path,
                    "note": "full CC limited to .py files",
                    "line_count": len(content.splitlines()),
                }
            )

        # OOM guard (audit fix #4)
        if len(content.encode()) > _DISK_MAX_BYTES:
            return json.dumps(
                {"error": "file too large for complexity analysis", "limit_kb": _DISK_MAX_BYTES // 1024}
            )

        try:
            tree = ast.parse(content, filename=file_path)
            module_cc = _compute_cc(tree)
            nesting = _max_depth(tree)
            functions = _per_function_cc(tree)
        except (SyntaxError, RecursionError) as exc:
            # SyntaxError on invalid Python; RecursionError on pathologically deep nesting
            return json.dumps({"error": str(exc), "file": file_path})

        return json.dumps(
            {
                "file": file_path,
                "module_cc": module_cc,
                "max_nesting_depth": nesting,
                "functions": functions,
            }
        )


# =====================================================================
# CodeDiffTool
# =====================================================================


class CodeDiffInput(BaseModel):
    file_path: str = Field(description="Path to the file to compare (RAM buffer vs on-disk original).")
    context_lines: int = Field(default=3, ge=0, le=10, description="Context lines in unified diff.")


class CodeDiffTool(BaseTool):
    """Produce a unified diff comparing the in-RAM dirty buffer to the on-disk original.

    Use this to see exactly what the AI has changed in the current session without
    modifying anything. If the file exists only in RAM (new file), shows a full
    addition diff. If no RAM buffer exists, reports no pending changes.
    Diff output is capped at 300 lines via a lazy iterator (O(min(N, cap)) memory).
    """

    name: str = "diff_changes"
    description: str = (
        "Show a unified diff of what the AI has changed for a file in the current session. "
        "Compares the in-memory (dirty) buffer against the saved on-disk version. "
        "New files show a full addition diff; unchanged files report no pending changes."
    )
    args_schema: Type[BaseModel] = CodeDiffInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _workspace_root: str = PrivateAttr()
    _ram_reader: Callable[[str], Optional[str]] = PrivateAttr()
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        workspace_root: str,
        ram_reader: Callable[[str], Optional[str]],
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_root = workspace_root
        self._ram_reader = ram_reader
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("CodeDiffTool is async-only — use _arun().")

    async def _arun(self, file_path: str, context_lines: int = 3) -> str:
        import asyncio

        # If not in RAM, nothing to diff
        ram_content = self._ram_reader(file_path)
        if ram_content is None:
            return f"no pending changes for {file_path}"

        # Disk read in thread; FileNotFoundError → None → treat as empty original (new file)
        disk_content = await asyncio.to_thread(_jailed_disk_read, file_path, self._workspace_root)

        original_lines = (disk_content or "").splitlines(keepends=True)
        modified_lines = ram_content.splitlines(keepends=True)

        # Lazy iterator — O(min(N, _DIFF_MAX_LINES)) memory (audit fix #4)
        diff_iter = unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=context_lines,
        )
        diff_str = "".join(itertools.islice(diff_iter, _DIFF_MAX_LINES))

        if not diff_str:
            return "no changes detected (content identical)"

        return wrap_boundary(diff_str, self._boundary_provider)


# =====================================================================
# DependencyAuditTool
# =====================================================================


class DependencyAuditInput(BaseModel):
    manifest_hint: Optional[str] = Field(
        default=None,
        description="Optional fnmatch filter on manifest filenames, e.g. 'requirements*.txt'.",
    )


def _parse_requirements(content: str) -> List[str]:
    """Parse pip-style requirements.txt / setup.cfg [options] install_requires."""
    deps = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        # Strip inline comments and extras
        dep = line.split("#")[0].strip()
        dep = dep.split(";")[0].strip()  # environment markers
        if dep:
            deps.append(dep)
    return deps


def _parse_package_json(content: str) -> List[str]:
    """Parse package.json — returns dep names from dependencies + devDependencies."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    # Use .get(..., {}) on both keys — many packages omit one (audit fix: KeyError)
    deps: dict = data.get("dependencies", {}) | data.get("devDependencies", {})
    return [f"{name}@{ver}" for name, ver in deps.items()]


def _parse_pyproject_toml(content: str) -> List[str]:
    """Parse pyproject.toml [project] dependencies. Requires Python 3.11+ or tomli."""
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import,no-redef]
        except ImportError:
            return []
    try:
        data = tomllib.loads(content)
        return data.get("project", {}).get("dependencies", [])
    except Exception:  # noqa: BLE001
        return []


class DependencyAuditTool(BaseTool):
    """Parse dependency manifests and optionally check for CVEs.

    Scans the workspace for requirements.txt, pyproject.toml, setup.cfg, and
    package.json. Returns a structured JSON summary of all found dependencies.
    CVE lookup is available when a search callable is injected (e.g. brave-search
    MCP); without one, only the manifest parse is returned (cve_checked=false).
    """

    name: str = "audit_dependencies"
    description: str = (
        "Parse dependency manifests (requirements.txt, pyproject.toml, package.json) "
        "and return a structured list of dependencies. Optionally checks each dep for "
        f"CVEs when a search provider is available (capped at {_CVE_MAX_DEPS} deps)."
    )
    args_schema: Type[BaseModel] = DependencyAuditInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _workspace_root: str = PrivateAttr()
    _path_provider: Callable[[], Awaitable[List[str]]] = PrivateAttr()
    _ram_reader: Callable[[str], Optional[str]] = PrivateAttr()
    _search_fn: Optional[Callable[[str, int], Awaitable[str]]] = PrivateAttr(default=None)
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        workspace_root: str,
        path_provider: Callable[[], Awaitable[List[str]]],
        ram_reader: Callable[[str], Optional[str]],
        search_fn: Optional[Callable[[str, int], Awaitable[str]]] = None,
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_root = workspace_root
        self._path_provider = path_provider
        self._ram_reader = ram_reader
        self._search_fn = search_fn
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DependencyAuditTool is async-only — use _arun().")

    async def _arun(self, manifest_hint: Optional[str] = None) -> str:
        import asyncio
        import fnmatch

        all_paths = await self._path_provider()

        # Filter to known manifest filenames + optional hint
        manifest_paths = [
            p
            for p in all_paths
            if os.path.basename(p) in _MANIFEST_NAMES
            and (manifest_hint is None or fnmatch.fnmatch(os.path.basename(p), manifest_hint))
        ]

        manifests = []
        for mp in manifest_paths:
            # RAM-first, disk fallback (both jailed)
            content = self._ram_reader(mp)
            if content is None:
                content = await asyncio.to_thread(_jailed_disk_read, mp, self._workspace_root)
            if content is None:
                continue

            name = os.path.basename(mp)
            if name in ("requirements.txt", "setup.cfg"):
                deps = _parse_requirements(content)
            elif name == "package.json":
                deps = _parse_package_json(content)
            elif name == "pyproject.toml":
                deps = _parse_pyproject_toml(content)
                if not deps:
                    # Fallback: return note if tomllib unavailable
                    manifests.append(
                        {"file": mp, "note": "TOML parsing requires Python 3.11+ or tomli", "deps": []}
                    )
                    continue
            else:
                continue

            manifests.append({"file": mp, "deps": deps})

        # Optional CVE lookup (brave-search MCP compatible)
        cve_results: List[dict] = []  # type: ignore[type-arg]
        cve_checked = False
        if self._search_fn is not None:
            cve_checked = True
            all_deps: List[str] = []
            for m in manifests:
                all_deps.extend(m.get("deps", []))
            for dep in all_deps[:_CVE_MAX_DEPS]:
                try:
                    result = await self._search_fn(f"CVE vulnerability {dep}", 3)
                    cve_results.append({"dep": dep, "result": result})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CVE search failed for %s: %s", dep, exc, exc_info=True)

        payload = json.dumps(
            {"manifests": manifests, "cve_checked": cve_checked, "cve_results": cve_results}
        )
        return wrap_boundary(payload, self._boundary_provider)


# =====================================================================
# WebSearchTool
# =====================================================================


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query string.")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum results to return (1-10).")


class WebSearchTool(BaseTool):
    """Run a web search using an injected search provider (brave-search MCP compatible).

    When no search provider is configured, returns a safe degradation message.
    The search callable signature is (query: str, max_results: int) -> str and
    matches the brave-search MCP server's `search` tool action.
    """

    name: str = "web_search"
    description: str = (
        "Search the web for up-to-date information, CVEs, API docs, or release notes. "
        "Requires a search provider to be configured; degrades gracefully when unavailable."
    )
    args_schema: Type[BaseModel] = WebSearchInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _search_fn: Optional[Callable[[str, int], Awaitable[str]]] = PrivateAttr(default=None)
    _boundary_provider: Optional[Callable[[], str]] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        search_fn: Optional[Callable[[str, int], Awaitable[str]]] = None,
        boundary_provider: Optional[Callable[[], str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._search_fn = search_fn
        self._boundary_provider = boundary_provider

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("WebSearchTool is async-only — use _arun().")

    async def _arun(self, query: str, max_results: int = 5) -> str:
        if self._search_fn is None:
            return "search provider unavailable"
        result = await self._search_fn(query, max_results)
        return wrap_boundary(result, self._boundary_provider)


# =====================================================================
# TokenLedgerReadTool
# =====================================================================


class TokenLedgerInput(BaseModel):
    tier: Literal["all", "local", "cloud"] = Field(
        default="all",
        description="Which tier to report: 'all' for full snapshot, 'local' or 'cloud' for filtered view.",
    )


class TokenLedgerReadTool(BaseTool):
    """Read the live token-cost counters from TokenLedger (not from graph state).

    Returns a JSON snapshot of prompt + completion tokens and estimated USD cost.
    Uses the module-level TokenLedger singleton which is updated by every LLM call
    in the cognitive engine — gives the Analyst a real-time view of token spend.
    """

    name: str = "read_token_ledger"
    description: str = (
        "Read live token usage and estimated cost from the TokenLedger. "
        "Tier 'all' returns full stats; 'local' or 'cloud' returns filtered counters."
    )
    args_schema: Type[BaseModel] = TokenLedgerInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _ledger: TokenLedger = PrivateAttr()

    def __init__(self, *, ledger: TokenLedger, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ledger = ledger

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TokenLedgerReadTool is async-only — use _arun().")

    async def _arun(self, tier: str = "all") -> str:
        snapshot = self._ledger.snapshot()

        if tier == "local":
            filtered = {k: v for k, v in snapshot.items() if "local" in k or "savings" in k}
        elif tier == "cloud":
            filtered = {k: v for k, v in snapshot.items() if "cloud" in k or "invested" in k}
        else:
            filtered = snapshot

        return json.dumps(filtered, indent=2)


# =====================================================================
# DeadCodeDetectionTool
# =====================================================================


class DeadCodeInput(BaseModel):
    """No arguments — scans the whole project's dependency graph."""


class DeadCodeDetectionTool(BaseTool):
    """Surface zero-resolved-in-degree, non-entrypoint in-repo files as dead-code candidates.

    Computed over the file-level dependency_graph. A hardcoded entrypoint set
    (FastAPI routes, pytest files, main.py, tool-registration call sites) is
    always excluded; an optional .ailienant/dead-code-allowlist.json extends the
    exclusion with user glob patterns over workspace-relative file paths. An
    absent or malformed allowlist falls back to the hardcoded set only — never
    raises. A candidate is a coarse, file-level signal for human review, not a
    guarantee that the file is unused.
    """

    name: str = "detect_dead_code"
    description: str = (
        "Find files with zero resolved in-degree in the dependency graph that are not a "
        "known entrypoint (FastAPI route, pytest file, main.py, tool registration) or "
        "allowlisted via .ailienant/dead-code-allowlist.json. Returns a JSON list of "
        "candidate dead-code files (workspace-relative paths) with in_degree 0."
    )
    args_schema: Type[BaseModel] = DeadCodeInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _project_id: str = PrivateAttr()
    _workspace_root: str = PrivateAttr()
    _session_id: Optional[str] = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        project_id: str,
        workspace_root: str,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._project_id = project_id
        self._workspace_root = workspace_root
        self._session_id = session_id

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DeadCodeDetectionTool is async-only — use _arun().")

    async def _arun(self) -> str:
        from core.dead_code import compute_dead_code

        try:
            candidates = await compute_dead_code(self._project_id, self._workspace_root, self._session_id)
        except Exception as exc:  # noqa: BLE001 — analyst surface must never crash the agent turn
            logger.warning("detect_dead_code failed for project %s: %s", self._project_id, exc, exc_info=True)
            return json.dumps({"error": "dead-code scan failed", "candidates": []})
        return json.dumps({"candidates": candidates, "count": len(candidates)})


# =====================================================================
# Schema registration helper + register_analyst_tools
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    json_schema_class: Type[BaseModel],
    *,
    roles: FrozenSet[str] = _ANALYST_ROLES,
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=roles,
    )


async def register_analyst_tools(store: ToolRAGStore) -> int:
    """Register all 9 analyst-scoped schemas in the given store. Returns count."""
    from tools.perception_tools import ArchitectureDigestInput, FindSymbolCallersInput

    schemas: List[ToolSchema] = [
        _tool_schema(
            "run_linter",
            "Lint a Python or TypeScript file with ruff/eslint. Returns JSON diagnostics.",
            RunLinterInput,
        ),
        _tool_schema(
            "analyze_complexity",
            "Compute McCabe cyclomatic complexity and nesting depth for a Python file.",
            ComplexityInput,
        ),
        _tool_schema(
            "audit_dependencies",
            "Parse dependency manifests and optionally check deps for CVEs.",
            DependencyAuditInput,
        ),
        _tool_schema(
            "diff_changes",
            "Unified diff of the in-session dirty buffer vs the on-disk original.",
            CodeDiffInput,
        ),
        _tool_schema(
            "web_search",
            "Search the web for CVEs, release notes, or API documentation.",
            WebSearchInput,
        ),
        _tool_schema(
            "read_token_ledger",
            "Read live token usage and estimated USD cost from the TokenLedger.",
            TokenLedgerInput,
            roles=_ANALYST_AND_ORCHESTRATOR,
        ),
        _tool_schema(
            "detect_dead_code",
            "Find zero-in-degree, non-entrypoint files in the dependency graph as dead-code candidates.",
            DeadCodeInput,
        ),
        _tool_schema(
            "architecture_digest",
            "Bounded project overview from the dependency graph: languages, top modules, "
            "centrality hotspots, community clusters, entrypoints, and node/edge counts.",
            ArchitectureDigestInput,
        ),
        _tool_schema(
            "find_symbol_callers",
            "Find files that call/reference a function/class/method by name, with a "
            "confidence tier per caller. Advisory — an empty result never means 'dead'.",
            FindSymbolCallersInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)


# =====================================================================
# Factories — construct the search-backed tools with a live provider
# =====================================================================


def make_web_search_tool(
    *,
    search_fn: Optional[Callable[[str, int], Awaitable[str]]] = None,
    boundary_provider: Optional[Callable[[], str]] = None,
) -> WebSearchTool:
    """Build a WebSearchTool wired to the brave-search MCP provider by default.

    With no explicit ``search_fn`` the tool is backed by the lazily-resolved
    brave-search session (see tools.mcp_adapter.make_brave_search_fn); the tool
    still degrades to its "unavailable" string until that session connects.
    """
    if search_fn is None:
        from tools.mcp_adapter import make_brave_search_fn  # local — avoid cycle

        search_fn = make_brave_search_fn()
    return WebSearchTool(search_fn=search_fn, boundary_provider=boundary_provider)


def make_dependency_audit_tool(
    *,
    workspace_root: str,
    path_provider: Callable[[], Awaitable[List[str]]],
    ram_reader: Callable[[str], Optional[str]],
    search_fn: Optional[Callable[[str, int], Awaitable[str]]] = None,
    boundary_provider: Optional[Callable[[], str]] = None,
) -> DependencyAuditTool:
    """Build a DependencyAuditTool with CVE lookup wired to brave-search by default.

    The manifest parse always runs; CVE enrichment becomes live the moment the
    brave-search session connects. Passing an explicit ``search_fn`` overrides the
    default (e.g. a stub in tests).
    """
    if search_fn is None:
        from tools.mcp_adapter import make_brave_search_fn  # local — avoid cycle

        search_fn = make_brave_search_fn()
    return DependencyAuditTool(
        workspace_root=workspace_root,
        path_provider=path_provider,
        ram_reader=ram_reader,
        search_fn=search_fn,
        boundary_provider=boundary_provider,
    )


# =====================================================================
# Callable registry — the name → executable map for the dispatch loop
# =====================================================================


def build_analyst_tools(state: Mapping[str, Any]) -> Dict[str, "RegisteredTool"]:
    """Construct the nine analyst tools bound to live session context.

    Mirrors the coder's ``make_coder_execute_tools``: the metadata-only schemas
    registered in the RAG store are inert; this is where the executable callables
    are instantiated with their state-injected providers and paired with the tier
    + role metadata the dispatch gate consults. Returns a ``name → RegisteredTool``
    map keyed by the same names ``register_analyst_tools`` registers.

    Heavy providers resolve lazily (no DB/network I/O at construction): the VFS
    RAM reader, the RAM ∪ catalog path provider, and the brave-search callable
    all defer their work to first use.
    """
    from core.tool_dispatch import RegisteredTool
    from core.token_ledger import token_ledger
    from core.vfs_middleware import VFSMiddleware
    from tools.perception_tools import ArchitectureDigestTool, FindSymbolCallersTool
    from tools.researcher_tools import make_vfs_path_provider

    workspace_root = str(state.get("workspace_root") or "")
    project_id = str(state.get("project_id") or "")
    session_id = state.get("session_id")

    vfs = VFSMiddleware()
    ram_reader = vfs.read_ram_only
    path_provider = make_vfs_path_provider(project_id, vfs=vfs)

    return {
        "run_linter": RegisteredTool(
            RunLinterTool(workspace_root=workspace_root, ram_reader=ram_reader),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "analyze_complexity": RegisteredTool(
            ComplexityAnalysisTool(workspace_root=workspace_root, ram_reader=ram_reader),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "diff_changes": RegisteredTool(
            CodeDiffTool(workspace_root=workspace_root, ram_reader=ram_reader),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "audit_dependencies": RegisteredTool(
            make_dependency_audit_tool(
                workspace_root=workspace_root,
                path_provider=path_provider,
                ram_reader=ram_reader,
            ),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "web_search": RegisteredTool(
            make_web_search_tool(),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "read_token_ledger": RegisteredTool(
            TokenLedgerReadTool(ledger=token_ledger),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_AND_ORCHESTRATOR,
        ),
        "detect_dead_code": RegisteredTool(
            DeadCodeDetectionTool(
                project_id=project_id,
                workspace_root=workspace_root,
                session_id=str(session_id) if session_id else None,
            ),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "architecture_digest": RegisteredTool(
            ArchitectureDigestTool(
                project_id=project_id,
                workspace_root=workspace_root,
                session_id=str(session_id) if session_id else None,
            ),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
        "find_symbol_callers": RegisteredTool(
            FindSymbolCallersTool(
                project_id=project_id,
                workspace_root=workspace_root,
                session_id=str(session_id) if session_id else None,
            ),
            ToolPrivilegeTier.READ_ONLY,
            _ANALYST_ROLES,
        ),
    }
