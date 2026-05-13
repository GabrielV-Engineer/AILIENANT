from __future__ import annotations

import os
from dataclasses import dataclass
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
