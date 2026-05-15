# ailienant-core/tests/test_vfs_transactions.py
#
# Phase 2.22.3 & 2.22.4 DoD: pytest tests/test_vfs_transactions.py -v -> 0 failures.
#
# Coverage:
#   apply_patch_to_vfs (OCC + diff):
#     1. OCC raises StaleFileException when hash mismatches
#     2. OCC passes when hash matches
#     3. Successful patch returns a valid unified diff string
#   make_patch_file_tool (OCC integration + IPC bridge):
#     4. StaleFileException caught -> LLM-readable error string returned
#     5. emit_patch callback fires with correct (file_path, diff, mode) args

import pytest

from core.exceptions import StaleFileException
from tools.patch_tool import _compute_hash, apply_patch_to_vfs, make_patch_file_tool


def _make_storage(content: str) -> dict:
    return {"foo.py": content}


# ---------------------------------------------------------------------------
# apply_patch_to_vfs — OCC
# ---------------------------------------------------------------------------


def test_occ_raises_stale_file_exception() -> None:
    original = "def foo():\n    return 1\n"
    concurrent_edit = "def foo():\n    return 999\n"
    storage = _make_storage(concurrent_edit)  # IDE already changed the file
    expected_hash = _compute_hash(original)   # hash captured before the edit

    with pytest.raises(StaleFileException, match="OCC conflict"):
        apply_patch_to_vfs(
            lambda p: storage.get(p),
            lambda p, c: storage.update({p: c}),
            "foo.py",
            "return 1",
            "return 42",
            expected_hash=expected_hash,
        )


def test_occ_passes_when_hash_matches() -> None:
    content = "def foo():\n    return 1\n"
    storage = _make_storage(content)
    expected_hash = _compute_hash(content)

    diff = apply_patch_to_vfs(
        lambda p: storage.get(p),
        lambda p, c: storage.update({p: c}),
        "foo.py",
        "return 1",
        "return 42",
        expected_hash=expected_hash,
    )
    assert "return 42" in storage["foo.py"]
    assert isinstance(diff, str)


# ---------------------------------------------------------------------------
# apply_patch_to_vfs — Unified Diff generation
# ---------------------------------------------------------------------------


def test_apply_patch_to_vfs_returns_unified_diff() -> None:
    content = "def foo():\n    return 1\n"
    storage = _make_storage(content)

    diff = apply_patch_to_vfs(
        lambda p: storage.get(p),
        lambda p, c: storage.update({p: c}),
        "foo.py",
        "return 1",
        "return 42",
    )
    assert "@@" in diff
    assert "-    return 1" in diff
    assert "+    return 42" in diff


# ---------------------------------------------------------------------------
# make_patch_file_tool — StaleFileException -> LLM string
# ---------------------------------------------------------------------------


def test_stale_file_exception_caught_by_tool() -> None:
    original = "def foo():\n    return 1\n"
    modified = "def foo():\n    return 999\n"
    storage = _make_storage(modified)  # file already changed in the IDE
    expected_hash = _compute_hash(original)

    tool = make_patch_file_tool(
        vfs_read=lambda p: storage.get(p),
        vfs_write=lambda p, c: storage.update({p: c}),
        expected_hash_provider=lambda p: expected_hash,
    )
    result = tool.invoke({
        "file_path": "foo.py",
        "search_block": "def foo():\n    return 1\n",
        "replace_block": "def foo():\n    return 42\n",
    })
    assert "modified" in result.lower()
    assert "re-read" in result.lower()


# ---------------------------------------------------------------------------
# make_patch_file_tool — IPC bridge callback (Phase 2.22.4)
# ---------------------------------------------------------------------------


def test_emit_patch_callback_fires_with_correct_args() -> None:
    content = "def foo():\n    return 1\n"
    storage = _make_storage(content)
    emitted: list = []

    tool = make_patch_file_tool(
        vfs_read=lambda p: storage.get(p),
        vfs_write=lambda p, c: storage.update({p: c}),
        emit_patch=lambda fp, diff, mode: emitted.append((fp, diff, mode)),
        mode="autonomous",
    )
    tool.invoke({
        "file_path": "foo.py",
        "search_block": "def foo():\n    return 1\n",
        "replace_block": "def foo():\n    return 42\n",
    })

    assert len(emitted) == 1
    fp, diff, mode = emitted[0]
    assert fp == "foo.py"
    assert "@@" in diff
    assert mode == "autonomous"
