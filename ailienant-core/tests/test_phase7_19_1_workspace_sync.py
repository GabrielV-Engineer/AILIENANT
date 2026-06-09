"""Directed test suite for Phase 7.19.1 — Workspace Synchronization Engine.

Uses asyncio.run() per test (no anyio dependency, matching project convention).
A StubSyncSurface provides an in-memory work surface for the majority of tests;
Docker-specific tests use a lightweight MockDockerContainer.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from core.blob_storage import ContentAddressableStorage
from core.workspace_sync import (
    DockerSyncSurface,
    LocalFsSyncSurface,
    SurfaceFile,
    SyncSurface,
    WorkspaceSnapshot,
    _content_hash,
    _raw_sha256,
    pull_surface_to_vfs,
    push_vfs_to_surface,
)
from brain.state import VFSFile


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_vfs_file(content: str, blob_store: ContentAddressableStorage) -> VFSFile:
    blob_hash = blob_store.put(content)
    return VFSFile(
        blob_hash=blob_hash,
        document_version_id=_content_hash(content),
        is_dirty=True,
    )


def _snapshot_from_dict(
    files: Dict[str, str],
    version_ids: Optional[Dict[str, str]] = None,
) -> WorkspaceSnapshot:
    """Build a WorkspaceSnapshot where chash = sha256 of UTF-8 encoded content."""
    snap = WorkspaceSnapshot()
    for path, content in files.items():
        raw = content.encode("utf-8")
        snap.files[path] = SurfaceFile(path=path, chash=_raw_sha256(raw))
    snap.version_ids = {p: _content_hash(c) for p, c in files.items()}
    if version_ids:
        snap.version_ids.update(version_ids)
    return snap


# ── In-memory stub surface ────────────────────────────────────────────────────


class StubSyncSurface(SyncSurface):
    """Fully in-memory SyncSurface for testing without filesystem or Docker."""

    def __init__(self, initial: Optional[Dict[str, bytes]] = None) -> None:
        self._files: Dict[str, bytes] = dict(initial or {})
        self.read_file_calls: List[str] = []

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        self.read_file_calls.append(rel_path)
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}

    def set_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content

    def delete_file(self, rel_path: str) -> None:
        self._files.pop(rel_path, None)


# ── DoD test 1: cell edit materialized ────────────────────────────────────────


def test_cell_edit_materialized() -> None:
    """Push writes VFS content to surface so next command sees the edit."""
    async def body() -> None:
        store = ContentAddressableStorage()
        vfs = {"src/foo.py": _make_vfs_file("def hello(): pass\n", store)}
        version_ids = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        await push_vfs_to_surface(surface, vfs, store, version_ids)

        raw = await surface.read_file("src/foo.py")
        assert raw is not None
        assert raw.decode("utf-8") == "def hello(): pass\n"

    asyncio.run(body())


# ── DoD test 2: sandbox change captured ───────────────────────────────────────


def test_sandbox_change_pulled_back() -> None:
    """A file modified on the surface after push is captured by pull."""
    async def body() -> None:
        store = ContentAddressableStorage()
        original = "x = 1\n"
        modified = "x = 42\n"
        vfs = {"mod.py": _make_vfs_file(original, store)}
        vid = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        before = await push_vfs_to_surface(surface, vfs, store, vid)
        # simulate sandbox modifying the file
        surface.set_file("mod.py", modified.encode("utf-8"))

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, vid, store
        )

        assert "mod.py" in new_files
        assert conflicts == []
        assert deleted == []
        pulled = store.get(new_files["mod.py"].blob_hash)
        assert pulled == modified
        assert new_files["mod.py"].document_version_id == _content_hash(modified)
        assert new_files["mod.py"].is_dirty is True

    asyncio.run(body())


# ── DoD test 3: unchanged file excluded, read_file not called ─────────────────


def test_unchanged_file_not_in_result() -> None:
    """Unchanged files do not appear in new_files and read_file is never called."""
    async def body() -> None:
        store = ContentAddressableStorage()
        content = "unchanged content\n"
        vfs = {"stable.py": _make_vfs_file(content, store)}
        vid = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        before = await push_vfs_to_surface(surface, vfs, store, vid)
        # surface unchanged — read_file_calls should remain empty
        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, vid, store
        )

        assert "stable.py" not in new_files
        assert conflicts == []
        assert deleted == []
        assert "stable.py" not in surface.read_file_calls

    asyncio.run(body())


# ── DoD test 4: concurrent edit triggers OCC guard ────────────────────────────


def test_concurrent_edit_triggers_occ() -> None:
    """A concurrent user edit bumps document_version_id → path goes to conflicts."""
    async def body() -> None:
        store = ContentAddressableStorage()
        original = "v1\n"
        vfs = {"race.py": _make_vfs_file(original, store)}
        vid_at_push = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        before = await push_vfs_to_surface(surface, vfs, store, vid_at_push)
        # sandbox modifies the file
        surface.set_file("race.py", b"v2\n")
        # simulate user concurrently editing → newer version_id
        vid_now = {"race.py": "9999_concurrent_edit"}

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, vid_now, store
        )

        assert "race.py" in conflicts
        assert "race.py" not in new_files
        assert deleted == []

    asyncio.run(body())


# ── DoD test 5: sandbox deletion propagated ───────────────────────────────────


def test_sandbox_deletion_propagated() -> None:
    """A file deleted on the surface after push appears in deleted_paths."""
    async def body() -> None:
        store = ContentAddressableStorage()
        vfs = {"old.py": _make_vfs_file("to be removed\n", store)}
        vid = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        before = await push_vfs_to_surface(surface, vfs, store, vid)
        surface.delete_file("old.py")

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, vid, store
        )

        assert "old.py" in deleted
        assert "old.py" not in new_files
        assert conflicts == []

    asyncio.run(body())


# ── DoD test 6: concurrent edit blocks deletion ───────────────────────────────


def test_concurrent_edit_blocks_deletion() -> None:
    """Concurrent user edit blocks a sandbox deletion — goes to conflicts."""
    async def body() -> None:
        store = ContentAddressableStorage()
        vfs = {"keep.py": _make_vfs_file("important\n", store)}
        vid_at_push = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        before = await push_vfs_to_surface(surface, vfs, store, vid_at_push)
        surface.delete_file("keep.py")
        vid_now = {"keep.py": "9999_concurrent_user_edit"}

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, vid_now, store
        )

        assert "keep.py" in conflicts
        assert "keep.py" not in deleted
        assert "keep.py" not in new_files

    asyncio.run(body())


# ── DoD test 7: push memory footprint is flat (O(1) per file) ─────────────────


def test_push_memory_flat() -> None:
    """blob_store.get() is called once per file — content never all-at-once."""
    async def body() -> None:
        get_calls: List[str] = []

        class _TrackingStore(ContentAddressableStorage):
            def get(self, blob_hash: str) -> Optional[str]:
                get_calls.append(blob_hash)
                return super().get(blob_hash)

        store = _TrackingStore()
        files = {
            "a.py": "a\n",
            "b.py": "b\n",
            "c.py": "c\n",
        }
        vfs = {p: _make_vfs_file(c, store) for p, c in files.items()}
        vid = {p: f.document_version_id for p, f in vfs.items()}
        surface = StubSyncSurface()

        await push_vfs_to_surface(surface, vfs, store, vid)

        # Each blob retrieved exactly once; no batch accumulation
        assert len(get_calls) == 3
        assert sorted(get_calls) == sorted(f.blob_hash for f in vfs.values())

    asyncio.run(body())


# ── DoD test 8: Docker exec_run called once, get_archive only for changed ──────


class _MockExecResult:
    def __init__(self, exit_code: int, stdout: bytes) -> None:
        self.exit_code = exit_code
        self.output = (stdout, b"")  # demux=True tuple (stdout, stderr)


def _make_tar_bytes(filename: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo(name=filename)
        ti.size = len(content)
        tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def test_get_file_hashes_single_exec() -> None:
    """DockerSyncSurface: one exec_run call; get_archive only for changed files."""
    async def body() -> None:
        container = MagicMock()
        # 5-file surface: file1/3/5 unchanged, file2/4 changed
        unchanged_content = b"stable"
        changed_content = b"modified"
        sha_unchanged = _raw_sha256(unchanged_content)
        sha_changed = _raw_sha256(changed_content)

        sha256_output = "\n".join([
            f"{sha_unchanged}  /work/file1.py",
            f"{sha_changed}  /work/file2.py",
            f"{sha_unchanged}  /work/file3.py",
            f"{sha_changed}  /work/file4.py",
            f"{sha_unchanged}  /work/file5.py",
        ]).encode("utf-8")

        container.exec_run.return_value = _MockExecResult(0, sha256_output)
        container.get_archive.return_value = (
            [_make_tar_bytes("file2.py", changed_content)], {}
        )

        surface = DockerSyncSurface(container, "/work")

        store = ContentAddressableStorage()
        # Build before snapshot (file1-5 at unchanged hash, file2/4 will change)
        before = WorkspaceSnapshot()
        for name in ("file1.py", "file2.py", "file3.py", "file4.py", "file5.py"):
            before.files[name] = SurfaceFile(
                path=name, chash=sha_unchanged
            )
            before.version_ids[name] = "v1"

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, {}, store
        )

        # exec_run called exactly once
        assert container.exec_run.call_count == 1
        # get_archive called only for changed files (file2 and file4)
        assert container.get_archive.call_count == 2
        changed_paths = {c.args[0] for c in container.get_archive.call_args_list}
        assert "/work/file2.py" in changed_paths
        assert "/work/file4.py" in changed_paths

    asyncio.run(body())


# ── DoD test 9: Docker surface targets /work, never /workspace ─────────────────


def test_docker_surface_targets_work() -> None:
    """DockerSyncSurface write_file uses /work as the put_archive target."""
    async def body() -> None:
        container = MagicMock()
        container.put_archive.return_value = True
        surface = DockerSyncSurface(container, "/work")

        await surface.write_file("src/foo.py", b"content")

        assert container.put_archive.called
        path_arg = container.put_archive.call_args[0][0]
        assert path_arg == "/work"
        assert path_arg != "/workspace"

    asyncio.run(body())


# ── DoD test 10: binary extension skipped ─────────────────────────────────────


def test_binary_extension_skipped() -> None:
    """Binary-extension files on the surface are not pulled to VFS."""
    async def body() -> None:
        store = ContentAddressableStorage()
        before = WorkspaceSnapshot()
        # No matching file in before — this is a new binary produced by sandbox
        surface = StubSyncSurface({"output.pyc": b"\x00\x01\x02"})

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, {}, store
        )

        assert "output.pyc" not in new_files
        assert "output.pyc" not in conflicts

    asyncio.run(body())


# ── DoD test 11: large file skipped ───────────────────────────────────────────


def test_large_file_skipped() -> None:
    """Files exceeding 500 KB are silently skipped during pull."""
    async def body() -> None:
        store = ContentAddressableStorage()
        large_content = b"x" * (500 * 1024 + 1)  # just over limit
        surface = StubSyncSurface({"big.txt": large_content})
        before = WorkspaceSnapshot()

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, {}, store
        )

        assert "big.txt" not in new_files
        assert "big.txt" not in conflicts
        assert "big.txt" not in deleted

    asyncio.run(body())


# ── DoD test 12: CRLF/LF OCC token parity ─────────────────────────────────────


def test_eol_normalization() -> None:
    """CRLF and LF variants produce the same document_version_id (OCC token)."""
    lf_content = "line1\nline2\n"
    crlf_content = "line1\r\nline2\r\n"

    # content_hash normalizes EOL — both must yield the same token
    assert _content_hash(lf_content) == _content_hash(crlf_content)

    # raw SHA-256 correctly identifies them as different bytes on the surface
    assert _raw_sha256(lf_content.encode("utf-8")) != _raw_sha256(
        crlf_content.encode("utf-8")
    )


# ── DoD test 13: directory traversal rejected ─────────────────────────────────


def test_path_traversal_rejected() -> None:
    """LocalFsSyncSurface rejects rel_path that escapes the surface root."""
    async def body() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            surface = LocalFsSyncSurface(tmpdir)
            with pytest.raises(ValueError, match="Path traversal"):
                await surface.write_file("../etc/passwd", b"root:x:0:0")

    asyncio.run(body())


# ── Bonus: new file created by sandbox (not in before) ────────────────────────


def test_new_file_created_by_sandbox() -> None:
    """A file created by the sandbox that was not in the push is captured."""
    async def body() -> None:
        store = ContentAddressableStorage()
        before = WorkspaceSnapshot()  # empty — nothing was pushed
        surface = StubSyncSurface({"new_output.py": b"result = 42\n"})

        new_files, conflicts, deleted = await pull_surface_to_vfs(
            surface, before, {}, store
        )

        assert "new_output.py" in new_files
        assert conflicts == []
        assert deleted == []
        assert store.get(new_files["new_output.py"].blob_hash) == "result = 42\n"

    asyncio.run(body())


# ── Bonus: missing blob in store does not crash push ──────────────────────────


def test_push_skips_evicted_blob() -> None:
    """push_vfs_to_surface skips files whose blob was evicted from the CAS."""
    async def body() -> None:
        store = ContentAddressableStorage()
        # Manually create a VFSFile with a non-existent blob_hash
        ghost_file = VFSFile(
            blob_hash="0" * 128,
            document_version_id="v0",
            is_dirty=True,
        )
        vfs = {"ghost.py": ghost_file}
        surface = StubSyncSurface()

        snapshot = await push_vfs_to_surface(surface, vfs, store, {"ghost.py": "v0"})

        # Skipped: nothing written to surface, nothing in snapshot
        assert "ghost.py" not in snapshot.files
        assert await surface.read_file("ghost.py") is None

    asyncio.run(body())
