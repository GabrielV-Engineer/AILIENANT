# tests/test_virtual_doc.py
"""Phase 3.4.4 DoD #3 — VirtualDocumentProvider shadows physical files with CAS blobs."""
from __future__ import annotations

import os

from core.blob_storage import blob_storage
from tools.validation.virtual_doc import VirtualDocumentProvider


def test_read_from_cas_when_shadowed() -> None:
    """A path in vfs_view is served from CAS (not disk)."""
    blob_hash = blob_storage.put("hello virtual world")
    provider = VirtualDocumentProvider({"src/foo.py": blob_hash})
    assert provider.read("src/foo.py") == "hello virtual world"


def test_read_from_disk_when_not_shadowed(tmp_path) -> None:
    """A path NOT in vfs_view falls back to disk."""
    target = tmp_path / "bar.py"
    target.write_text("disk content", encoding="utf-8")
    provider = VirtualDocumentProvider({})
    assert provider.read(str(target)) == "disk content"


def test_returns_none_when_cas_miss() -> None:
    """A shadowed path whose hash isn't in CAS returns None (does not raise)."""
    provider = VirtualDocumentProvider({"phantom.py": "deadbeefcafebabe"})
    assert provider.read("phantom.py") is None


def test_returns_none_when_disk_miss(tmp_path) -> None:
    """An unshadowed path that doesn't exist on disk returns None."""
    provider = VirtualDocumentProvider({})
    assert provider.read(str(tmp_path / "nope.txt")) is None


def test_path_normalisation_matches_cas() -> None:
    """vfs_view lookups are normpath-insensitive (./, mixed slashes)."""
    blob_hash = blob_storage.put("normalised")
    provider = VirtualDocumentProvider({"foo/bar.py": blob_hash})
    # Same logical path, different separator forms.
    assert provider.read(os.path.join("foo", "bar.py")) == "normalised"
    assert provider.read("foo/./bar.py") == "normalised"


def test_is_shadowed() -> None:
    """is_shadowed correctly reports vfs_view membership."""
    blob_hash = blob_storage.put("x")
    provider = VirtualDocumentProvider({"a/b.py": blob_hash})
    assert provider.is_shadowed("a/b.py") is True
    assert provider.is_shadowed(os.path.join("a", "b.py")) is True
    assert provider.is_shadowed("other.py") is False


def test_cas_shadow_takes_precedence_over_disk(tmp_path) -> None:
    """When both CAS and disk have a path, CAS wins."""
    target = tmp_path / "twins.py"
    target.write_text("from disk", encoding="utf-8")
    blob_hash = blob_storage.put("from cas")
    provider = VirtualDocumentProvider({str(target): blob_hash})
    assert provider.read(str(target)) == "from cas"
