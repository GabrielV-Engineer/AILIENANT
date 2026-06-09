"""Bidirectional VFS ↔ Sandbox workspace synchronization.

Provides two public functions and a SyncSurface strategy pair:

    push_vfs_to_surface  — write VFS dirty files to the sandbox work surface
    pull_surface_to_vfs  — map sandbox-modified files back to VFSFile records
                           with OCC guard and ghost-deletion detection

Change detection uses SHA-256 of raw bytes (matching sha256sum output) so a
single get_file_hashes() call (one exec_run for Docker, one rglob for local)
determines which files need to be fetched — read_file() is called only for the
changed subset.

OCC tokens (document_version_id) use content_hash() — SHA-256 of EOL-normalized
text — matching agents/coder.py so stale-guard comparisons are EOL-agnostic.
The two hash spaces are deliberately separate: raw-bytes hash for change
detection, normalized-text hash for OCC tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from brain.state import VFSFile
    from core.blob_storage import ContentAddressableStorage

logger = logging.getLogger("AILIENANT_WORKSPACE_SYNC")

_MAX_SURFACE_FILE_BYTES: int = 500 * 1024  # mirror VFSMiddleware ceiling

# Extension block-list mirrors vfs_middleware._BINARY_EXTENSIONS
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".svg", ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".wasm",
    ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".db", ".sqlite", ".pyc", ".class",
})


def _raw_sha256(data: bytes) -> str:
    """SHA-256 of raw bytes — identical to what sha256sum produces."""
    return hashlib.sha256(data).hexdigest()


def _content_hash(s: str) -> str:
    """SHA-256 of EOL-normalized text. Matches agents/coder.py:content_hash.

    Used exclusively for document_version_id OCC tokens — NOT for change
    detection. Normalizing EOL before hashing prevents CRLF/LF divergence
    from triggering false OCC conflicts across platforms.
    """
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_binary_extension(path: str) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXTENSIONS


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SurfaceFile:
    """Lightweight snapshot of a file on the work surface."""

    path: str   # slash-normalized relative path
    chash: str  # SHA-256 of raw bytes (what sha256sum produces)


@dataclass
class WorkspaceSnapshot:
    """State of the work surface captured immediately after a push."""

    files: Dict[str, SurfaceFile] = field(default_factory=dict)
    # path → document_version_id at push time (OCC anchor for pull)
    version_ids: Dict[str, str] = field(default_factory=dict)


# ── SyncSurface ABC ───────────────────────────────────────────────────────────


class SyncSurface(ABC):
    """Abstract R/W access to a sandbox's writable work surface.

    Implementations cover a local directory (NativeDirectSandboxAdapter) and
    Docker's /work tmpfs. The hot path is get_file_hashes(): implementations
    must return all hashes with the fewest possible I/O round-trips.
    """

    @abstractmethod
    async def write_file(self, rel_path: str, content: bytes) -> None:
        """Write content to rel_path on the surface (parent dirs created)."""
        ...

    @abstractmethod
    async def read_file(self, rel_path: str) -> Optional[bytes]:
        """Return raw bytes at rel_path, or None if absent / unreadable."""
        ...

    @abstractmethod
    async def get_file_hashes(self) -> Dict[str, str]:
        """Return {rel_path: sha256_hex} for all files on the surface.

        This is called once per pull_surface_to_vfs() invocation. Concrete
        implementations must minimize I/O calls:
          LocalFsSyncSurface  — rglob + local sha256 (no network)
          DockerSyncSurface   — single exec_run("find /work -exec sha256sum +")
        """
        ...


# ── LocalFsSyncSurface ────────────────────────────────────────────────────────


class LocalFsSyncSurface(SyncSurface):
    """Work surface = a local directory (NativeDirectSandboxAdapter cwd)."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    def _safe_path(self, rel_path: str) -> Path:
        """Resolve rel_path under root; raise ValueError on directory traversal."""
        rel_normalized = rel_path.lstrip("/").replace("\\", "/")
        resolved = (self._root / rel_normalized).resolve()
        root_str = str(self._root)
        resolved_str = str(resolved)
        # Allow exact root match or any path strictly under root
        if resolved_str != root_str and not resolved_str.startswith(root_str + os.sep):
            raise ValueError(
                f"Path traversal rejected: {rel_path!r} resolves outside "
                f"surface root {self._root}"
            )
        return resolved

    async def write_file(self, rel_path: str, content: bytes) -> None:
        target = self._safe_path(rel_path)
        await asyncio.to_thread(_write_bytes_sync, target, content)

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        target = self._safe_path(rel_path)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except (FileNotFoundError, OSError):
            return None

    async def get_file_hashes(self) -> Dict[str, str]:
        root = self._root
        return await asyncio.to_thread(_local_file_hashes_sync, root)


def _local_file_hashes_sync(root: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        result[rel] = _raw_sha256(data)
    return result


def _write_bytes_sync(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ── DockerSyncSurface ─────────────────────────────────────────────────────────


class DockerSyncSurface(SyncSurface):
    """Work surface = /work tmpfs inside a running Docker container."""

    def __init__(self, container: Any, workdir: str = "/work") -> None:
        self._container = container
        self._workdir = workdir.rstrip("/")

    def _safe_rel(self, rel_path: str) -> str:
        """Return a safe relative path string with .. components stripped."""
        parts: List[str] = []
        for part in rel_path.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                # silently drop traversal attempts that escape root
            else:
                parts.append(part)
        return "/".join(parts)

    def _container_path(self, rel_path: str) -> str:
        return f"{self._workdir}/{self._safe_rel(rel_path)}"

    async def write_file(self, rel_path: str, content: bytes) -> None:
        """Pack content into an in-memory tar and extract at workdir."""
        safe_rel = self._safe_rel(rel_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo(name=safe_rel)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
        buf.seek(0)
        await asyncio.to_thread(self._container.put_archive, self._workdir, buf)

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        container_path = self._container_path(rel_path)
        try:
            raw = await asyncio.to_thread(
                _docker_read_file_sync, self._container, container_path
            )
            return raw
        except Exception:  # noqa: BLE001 — container gone, path missing, etc.
            return None

    async def get_file_hashes(self) -> Dict[str, str]:
        """Single exec_run hashes all surface files — O(1) network round-trip."""
        cmd = f"find {self._workdir} -type f -exec sha256sum {{}} +"
        try:
            result = await asyncio.to_thread(
                self._container.exec_run,
                cmd,
                demux=True,
                stdout=True,
                stderr=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DockerSyncSurface.get_file_hashes failed: %s", exc)
            return {}

        exit_code = result.exit_code
        stdout_bytes: bytes = b""
        if result.output and isinstance(result.output, (tuple, list)):
            stdout_bytes = result.output[0] or b""
        elif isinstance(result.output, bytes):
            stdout_bytes = result.output

        if exit_code != 0 and not stdout_bytes:
            return {}

        return _parse_sha256sum_output(stdout_bytes, self._workdir)


def _docker_read_file_sync(container: Any, container_path: str) -> Optional[bytes]:
    bits, _ = container.get_archive(container_path)
    buf = io.BytesIO()
    for chunk in bits:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tf:
        members = tf.getmembers()
        if not members:
            return None
        f = tf.extractfile(members[0])
        return f.read() if f is not None else None


def _parse_sha256sum_output(raw: bytes, workdir: str) -> Dict[str, str]:
    """Parse sha256sum output lines into {rel_path: sha256_hex}.

    sha256sum format: ``SHA256HEX  /absolute/path`` (two spaces, text mode)
    or ``SHA256HEX */absolute/path`` (one space then *, binary mode).
    """
    result: Dict[str, str] = {}
    prefix = workdir.rstrip("/") + "/"
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, path_part = parts
        # strip binary-mode '*' indicator
        if path_part.startswith("*"):
            path_part = path_part[1:]
        path_part = path_part.strip()
        # derive relative path
        if path_part.startswith(prefix):
            rel = path_part[len(prefix):]
        elif path_part.startswith("./"):
            # relative output from find — unlikely but handled
            cleaned = path_part[2:]
            wp = prefix.lstrip("/")
            rel = cleaned[len(wp):] if cleaned.startswith(wp) else cleaned
        else:
            rel = path_part
        if rel:
            result[rel] = sha
    return result


# ── Core sync functions ───────────────────────────────────────────────────────


async def push_vfs_to_surface(
    surface: SyncSurface,
    vfs_files: "Dict[str, VFSFile]",
    blob_store: "ContentAddressableStorage",
    version_ids: Dict[str, str],
) -> WorkspaceSnapshot:
    """Write VFS files to the sandbox work surface one file at a time.

    Retrieves each file's content from blob_store by blob_hash and writes to
    the surface immediately, then discards the reference — peak memory is
    O(1 file) regardless of workspace size. Files whose blob has been evicted
    from the store are skipped with a warning.

    Returns a WorkspaceSnapshot recording the raw-bytes SHA-256 of each
    written file and the push-time document_version_id for OCC anchoring.
    """
    snapshot = WorkspaceSnapshot()
    for path, vfs_file in vfs_files.items():
        content_str = blob_store.get(vfs_file.blob_hash)
        if content_str is None:
            logger.warning(
                "push_vfs_to_surface: blob %s.. evicted for %r — skipping",
                vfs_file.blob_hash[:8], path,
            )
            continue
        raw = content_str.encode("utf-8")
        try:
            await surface.write_file(path, raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("push_vfs_to_surface: write_file(%r) failed: %s", path, exc)
            continue
        snapshot.files[path] = SurfaceFile(path=path, chash=_raw_sha256(raw))
        snapshot.version_ids[path] = version_ids.get(path, vfs_file.document_version_id)
    return snapshot


async def pull_surface_to_vfs(
    surface: SyncSurface,
    before: WorkspaceSnapshot,
    current_version_ids: Dict[str, str],
    blob_store: "ContentAddressableStorage",
) -> Tuple["Dict[str, VFSFile]", List[str], List[str]]:
    """Map sandbox-modified files back to VFSFile records with OCC guard.

    Performs a three-way diff of the work surface against the push snapshot:

    1. Changed files  (present in after, hash differs from before)
       — fetched, decoded, stored in blob_store, returned as new VFSFile records
       — subject to OCC guard: skip if user/agent bumped document_version_id
         between push and now (path goes to conflicts list)

    2. Deleted files  (present in before, absent from after)
       — returned in deleted_paths for the caller to remove from vfs_buffer
       — subject to the same OCC guard

    3. Unchanged files (hash equal to before) — ignored entirely; read_file
       is NOT called for them (O(1) Docker latency guarantee)

    Binary files and files exceeding _MAX_SURFACE_FILE_BYTES are silently
    skipped (not returned, not in conflicts).

    Returns (new_vfs_files, conflict_paths, deleted_paths).
    """
    from brain.state import VFSFile

    after_hashes = await surface.get_file_hashes()

    new_files: Dict[str, VFSFile] = {}
    conflicts: List[str] = []
    deleted_paths: List[str] = []

    # ── Changed / new files ───────────────────────────────────────────────────
    for path, after_hash in after_hashes.items():
        before_file = before.files.get(path)
        if before_file is not None and after_hash == before_file.chash:
            continue  # unchanged — no read_file call

        before_vid = before.version_ids.get(path, "")
        current_vid = current_version_ids.get(path, "")
        if current_vid > before_vid:
            conflicts.append(path)
            continue

        if _is_binary_extension(path):
            continue

        raw = await surface.read_file(path)
        if raw is None:
            continue
        if len(raw) > _MAX_SURFACE_FILE_BYTES:
            logger.debug("pull_surface_to_vfs: %r exceeds size ceiling — skipped", path)
            continue

        content = raw.decode("utf-8", errors="replace")
        new_blob = blob_store.put(content)
        new_vid = _content_hash(content)
        new_files[path] = VFSFile(
            blob_hash=new_blob, document_version_id=new_vid, is_dirty=True
        )

    # ── Deleted files (ghost prevention) ─────────────────────────────────────
    for path in before.files:
        if path in after_hashes:
            continue  # still present
        before_vid = before.version_ids.get(path, "")
        current_vid = current_version_ids.get(path, "")
        if current_vid > before_vid:
            conflicts.append(path)
            continue
        deleted_paths.append(path)

    return new_files, conflicts, deleted_paths
