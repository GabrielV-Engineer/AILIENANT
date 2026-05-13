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


@dataclass
class IndexingResult:
    """Result returned by the worker process. All fields are primitives — picklable."""
    file_path: str
    symbol_count: int
    language_id: str
    success: bool
    error: Optional[str] = None
    imports: list[str] = field(default_factory=list)  # absolute module paths extracted from AST


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
    and unambiguously picklable across all Python versions.
    """
    edges: tuple[tuple[str, str], ...]  # (source_file, target_dependency)


@dataclass
class PPRResult:
    """PageRank scores returned by the worker process. Keys are file paths."""
    scores: dict[str, float]
    success: bool
    error: Optional[str] = None
