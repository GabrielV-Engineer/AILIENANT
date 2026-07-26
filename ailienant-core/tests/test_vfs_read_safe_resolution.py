"""Firewalled-reader path resolution — the backend must read the SAME bytes the
VS Code host hashes at apply time.

The write pipeline's stale-file guard compares a pre-edit hash captured backend-side
against the host's hash of ``doc.getText()``. The host resolves a relative path against
the workspace folder; the backend firewall must do the same, or a relative ``target_file``
reads as absent (hash of ""), diverges from the host's real-file hash, and the guard
false-positives ("these files changed since the proposal") on an unchanged file.
"""
from __future__ import annotations

from pathlib import Path

from agents.coder import content_hash
from core.vfs_middleware import make_safe_reader


def test_read_safe_resolves_relative_against_project_root(tmp_path: Path) -> None:
    f = tmp_path / "fibonacci.py"
    f.write_text("def fib(n):\n    return n\n", encoding="utf-8")
    reader = make_safe_reader("proj-rel", str(tmp_path), None)
    # A relative path resolves against project_root (the host's rule), not the CWD.
    assert reader("fibonacci.py") == "def fib(n):\n    return n\n"


def test_read_safe_absolute_path_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    reader = make_safe_reader("proj-abs", str(tmp_path), None)
    # Absolute paths are untouched by the resolution.
    assert reader(str(f)) == "x = 1\n"


def test_read_safe_relative_without_root_falls_back_to_cwd(tmp_path: Path) -> None:
    # With no project_root the behavior is unchanged (CWD-relative); a path that does
    # not exist there returns None via the firewall — no regression, no false read.
    reader = make_safe_reader("proj-none", None, None)
    assert reader("definitely_missing_file_zzz_qqq.py") is None


def test_relative_base_hash_matches_host_hash(tmp_path: Path) -> None:
    """The pre-edit hash captured for a relative path equals the host's hash over the
    real on-disk file (same EOL normalization) — so the stale guard passes."""
    f = tmp_path / "fibonacci.py"
    # CRLF on disk (Windows) — the host's _normalizeEol + the backend's content_hash
    # both collapse CRLF→LF before hashing, so the two hashes must agree.
    f.write_bytes("def fib(n):\r\n    return n\r\n".encode("utf-8"))
    reader = make_safe_reader("proj-hash", str(tmp_path), None)
    got = reader("fibonacci.py")
    assert got is not None
    host_like = content_hash("def fib(n):\r\n    return n\r\n")
    assert content_hash(got) == host_like
