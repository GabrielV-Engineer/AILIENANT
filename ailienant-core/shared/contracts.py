from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class IndexingRequest:
    """Cross-process indexing job. All fields are primitives — picklable by ProcessPoolExecutor."""
    file_path: str
    content: str
    language_id: str  # VS Code languageId: "python", "typescript", etc.
    workspace_root: str = ""  # absolute workspace root; confines lexical relative-specifier resolution


@dataclass(frozen=True)
class SymbolDef:
    """One symbol definition (function/class/method) extracted from a file's AST.

    All fields are primitives so the enclosing IndexingResult stays picklable across
    the ProcessPoolExecutor boundary — a tree-sitter node is process-local and must
    never cross it. ``qualified_name`` is a dotted FQN of the enclosing named scopes
    (module → class → method); ``start_line``/``end_line`` are 1-indexed.
    """
    qualified_name: str
    kind: str  # "function" | "class" | "method"
    start_line: int
    end_line: int


@dataclass
class IndexingResult:
    """Result returned by the worker process. All fields are primitives — picklable."""
    file_path: str
    symbol_count: int
    language_id: str
    success: bool
    error: Optional[str] = None
    imports: list[str] = field(default_factory=list)  # absolute module paths extracted from AST
    symbols: list[SymbolDef] = field(default_factory=list)  # symbol definitions extracted from AST


_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".sql": "sql",
    ".sh": "shellscript",
    ".ps1": "powershell",
}


def detect_language(file_path: str) -> str:
    """Map file extension → VS Code languageId. Returns '' for unsupported types."""
    ext = os.path.splitext(file_path)[1].lower()
    return _EXT_LANG.get(ext, "")


@dataclass(frozen=True)
class PPRRequest:
    """Dependency graph edges for one project. Sent to ProcessPoolExecutor for PageRank.

    Using a tuple of tuples (not list of lists) ensures the contract is immutable
    and unambiguously picklable across all Python versions. ``indexed_files`` is the
    set of source files known to the project, used by the analytics worker to resolve
    edge confidence (a target that is an indexed file is EXTRACTED, else INFERRED).
    """
    edges: tuple[tuple[str, str], ...]  # (source_file, target_dependency)
    indexed_files: tuple[str, ...] = ()  # project node universe for confidence resolution


@dataclass
class PPRResult:
    """PageRank scores returned by the worker process. Keys are file paths.

    ``communities`` maps node → Louvain community id; ``edge_confidence`` carries
    (source, target, confidence_label, confidence_score) per edge. Both default empty
    so the legacy PageRank-only path (calculate_ppr_sync) stays valid unchanged.
    """
    scores: dict[str, float]
    success: bool
    error: Optional[str] = None
    communities: dict[str, int] = field(default_factory=dict)
    edge_confidence: tuple[tuple[str, str, str, float], ...] = ()
