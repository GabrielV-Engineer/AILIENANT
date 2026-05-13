import os
import threading
from typing import Any, Dict, List, Optional

import pathspec
from pydantic import BaseModel

from .ast_engine import ASTEngine as _ASTEngine

_ast = _ASTEngine()


# Contract aligned with vfs_reader.ts
class DirtyBuffer(BaseModel):
    uri: str
    content: str
    version: int
    languageId: str


# =====================================================================
# VFS FIREWALL — Result Contract
# =====================================================================


class VFSReadResult(BaseModel):
    content: Optional[str] = None
    error: Optional[str] = None      # "FILE_IGNORED" | "BINARY_FILE" | "FILE_TOO_LARGE" | "MINIFIED" | "READ_ERROR"
    metadata: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# =====================================================================
# VFS FIREWALL — Constants
# =====================================================================

_MAX_FILE_BYTES: int = 500 * 1024   # 500 KB hard ceiling
_MAX_LINE_CHARS: int = 1000         # minification detection threshold

_BINARY_EXTENSIONS: frozenset = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".svg", ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".wasm",
    ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".db", ".sqlite", ".pyc", ".class",
})

# Per-project_id cache: {project_id: PathSpec}  — populated once per project on first read
_ignore_specs: Dict[str, "pathspec.PathSpec"] = {}
_ignore_specs_lock = threading.Lock()


# =====================================================================
# VFS MIDDLEWARE
# =====================================================================


class VFSMiddleware:
    """
    Virtual File System (VFS) Proxy.
    Single source of truth for file state during a cognitive mission.

    Tier 1: RAM lookup O(1) for unsaved IDE buffers.
    Tier 2: Disk I/O fallback with safe error handling.
    Firewall: 3-layer content filter applied via read_safe().
    """

    _instance = None
    _lock = threading.Lock()
    _ram_vfs: Dict[str, str]

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(VFSMiddleware, cls).__new__(cls)
                cls._instance._ram_vfs = {}
        return cls._instance

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_dirty_buffers(self, buffers: List[DirtyBuffer]) -> None:
        """
        Overwrite RAM with fresh IDE buffers.
        Time complexity: O(N) where N = number of dirty buffers.
        """
        with self._lock:
            self._ram_vfs.clear()
            for buf in buffers:
                normalized_path = os.path.normpath(buf.uri)
                self._ram_vfs[normalized_path] = buf.content
        # AST parse outside lock — CPU-bound, doesn't need _ram_vfs lock
        for buf in buffers:
            _ast.parse(os.path.normpath(buf.uri), buf.content, buf.languageId)

    # ------------------------------------------------------------------
    # Transparent read (backward-compatible, no firewall)
    # ------------------------------------------------------------------

    def read(self, filepath: str) -> str:
        """
        Transparent proxy read for LangGraph tools.
        RAM first, disk fallback. No firewall applied — callers that need
        filtering should use read_safe() instead.
        """
        normalized_path = os.path.normpath(filepath)

        with self._lock:
            if normalized_path in self._ram_vfs:
                return self._ram_vfs[normalized_path]

        try:
            with open(normalized_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"AILIENANT VFS Error: Archivo inexistente -> {normalized_path}"
            )
        except Exception as e:
            raise RuntimeError(f"AILIENANT VFS I/O Exception: {str(e)}")

    # ------------------------------------------------------------------
    # Firewalled read (context assembly, LLM injection)
    # ------------------------------------------------------------------

    def get_ast(self, filepath: str) -> "Optional[Any]":
        return _ast.get(os.path.normpath(filepath))

    def read_safe(
        self,
        filepath: str,
        project_id: Optional[str] = None,
        project_root: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> VFSReadResult:
        """
        Three-layer content firewall for LLM context assembly.

        Layer 1 — Ignore rules: respects .gitignore and .ailienantignore.
        Layer 2 — Binary block: rejects non-textual file extensions.
        Layer 3 — Anti-OOM: rejects files > 500 KB and minified files.

        Returns VFSReadResult; callers check .ok before using .content.
        """
        normalized = os.path.normpath(filepath)
        ext = os.path.splitext(normalized)[1].lower()
        size_meta: dict = {"path": normalized, "extension": ext}

        # Layer 1 — Ignore rules (O(1) after initial PathSpec compilation per project)
        if project_id and project_root:
            spec = self._load_ignore_spec(project_root, project_id)
            try:
                rel_path = os.path.relpath(normalized, project_root)
            except ValueError:
                rel_path = normalized  # Different drive on Windows — skip ignore check
            if spec.match_file(rel_path):
                return VFSReadResult(
                    error="FILE_IGNORED",
                    metadata={**size_meta, "reason": "matched .gitignore / .ailienantignore"},
                )

        # Layer 2 — Binary extension block
        if ext in _BINARY_EXTENSIONS:
            return VFSReadResult(
                error="BINARY_FILE",
                metadata={**size_meta, "reason": "non-textual extension"},
            )

        # Layer 3a — Hard size ceiling (disk files only; RAM buffers are IDE-controlled)
        with self._lock:
            in_ram = normalized in self._ram_vfs
        if not in_ram:
            try:
                file_size = os.path.getsize(normalized)
            except OSError:
                file_size = 0
            if file_size > _MAX_FILE_BYTES:
                return VFSReadResult(
                    error="FILE_TOO_LARGE",
                    metadata={
                        **size_meta,
                        "size_bytes": file_size,
                        "limit_bytes": _MAX_FILE_BYTES,
                    },
                )

        # Delegate to the base read() for the actual content
        try:
            content = self.read(normalized)
        except (FileNotFoundError, RuntimeError) as exc:
            return VFSReadResult(
                error="READ_ERROR",
                metadata={**size_meta, "detail": str(exc)},
            )

        # Layer 3b — Minification detection (any single line > 1000 chars)
        if any(len(line) > _MAX_LINE_CHARS for line in content.splitlines()):
            return VFSReadResult(
                error="MINIFIED",
                metadata={**size_meta, "reason": "line length exceeds minification threshold"},
            )

        result = VFSReadResult(content=content)
        if session_id:
            from .db import log_file_read_sync as _log
            _log(session_id, normalized, None)
        return result

    # ------------------------------------------------------------------
    # Ignore spec cache (thread-safe, compiled once per project_id)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_ignore_spec(project_root: str, project_id: str) -> "pathspec.PathSpec":
        """
        Parse .gitignore and .ailienantignore from project_root and cache the
        compiled PathSpec under project_id. Thread-safe — concurrent callers
        for the same project_id will block until the first parse completes.
        """
        with _ignore_specs_lock:
            if project_id in _ignore_specs:
                return _ignore_specs[project_id]
            lines: list = []
            for fname in (".gitignore", ".ailienantignore"):
                candidate = os.path.join(project_root, fname)
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        lines.extend(f.readlines())
                except FileNotFoundError:
                    pass
            spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
            _ignore_specs[project_id] = spec
            return spec
