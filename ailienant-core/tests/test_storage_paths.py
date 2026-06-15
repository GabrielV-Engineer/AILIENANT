"""The per-workspace project id must be stable under cosmetic path differences.

The on-disk GraphRAG/catalog stores are keyed by ``project_id_for(workspace_root)``.
If the digest shifted when a caller passed a differently-cased, differently-separated
or trailing-slashed spelling of the same folder, the lazy indexer would orphan the
existing index. These tests pin the normalization invariants; the extension's
``PathResolver.computeProjectId`` mirrors them byte-for-byte on the same host.
"""
from __future__ import annotations

import os
import sys

import pytest

from core.storage_paths import project_id_for


def test_trailing_slash_is_irrelevant() -> None:
    root = os.path.join(os.sep, "home", "u", "proj")
    assert project_id_for(root) == project_id_for(root + os.sep)
    assert project_id_for(root) == project_id_for(root + os.sep + os.sep)


def test_redundant_separators_collapse() -> None:
    # Relative base so doubling never turns a leading separator into a Windows UNC
    # root (\\server\share); only interior redundant separators are exercised.
    base = os.path.join("home", "u", "proj")
    doubled = base.replace(os.sep, os.sep + os.sep)
    assert project_id_for(base) == project_id_for(doubled)


def test_same_path_is_deterministic() -> None:
    root = os.path.join(os.sep, "var", "data", "app")
    assert project_id_for(root) == project_id_for(root)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path-casing/separator semantics")
def test_windows_casing_and_separators_are_irrelevant() -> None:
    assert project_id_for(r"C:\Projects\App") == project_id_for(r"c:\projects\app")
    assert project_id_for(r"C:\Projects\App") == project_id_for("C:/Projects/App")
    assert project_id_for(r"C:\Projects\App\\") == project_id_for(r"C:\Projects\App")
    assert project_id_for(r"C:\Projects\App\\\\") == project_id_for(r"C:\Projects\App")


@pytest.mark.skipif(sys.platform != "win32", reason="disk-root slash is Windows-specific")
def test_windows_disk_root_preserved_and_stable() -> None:
    # The disk root keeps its slash (normpath never strips `C:\` to `C:`), so the
    # root and a re-cased/forward-slash spelling of it collapse to one id.
    assert project_id_for("C:\\") == project_id_for("c:/")
    assert project_id_for("C:\\") == project_id_for("C:\\")
