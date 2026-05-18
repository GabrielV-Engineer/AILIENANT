# ailienant-core/tests/test_mutation_tools.py
#
# Phase 5.4 smoke tests for the WRITE-tier mutation bundle.
# Storage pattern mirrors tests/test_vfs_transactions.py (Phase 2.22):
# an in-process Dict[str, str] stands in for the VFS.

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore
from tools.agent_tools import make_state_aware_read_file_tool
from tools.mutation_tools import (
    AtomicCodePatchTool,
    BatchSemanticEditTool,
    FileWriteTool,
    _ALLOWED_MUTATION_ROLES,
    register_mutation_tools,
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _vfs(initial: Dict[str, str]) -> Dict[str, str]:
    return dict(initial)


def _patch_tool(storage: Dict[str, str]) -> AtomicCodePatchTool:
    return AtomicCodePatchTool(
        vfs_read=storage.get,
        vfs_write=lambda p, c: storage.__setitem__(p, c),
    )


def _batch_tool(storage: Dict[str, str]) -> BatchSemanticEditTool:
    return BatchSemanticEditTool(
        vfs_read=storage.get,
        vfs_write=lambda p, c: storage.__setitem__(p, c),
    )


def _write_tool(storage: Dict[str, str]) -> FileWriteTool:
    return FileWriteTool(
        vfs_read=storage.get,
        vfs_write=lambda p, c: storage.__setitem__(p, c),
    )


# =====================================================================
# AtomicCodePatchTool
# =====================================================================


@pytest.mark.anyio
async def test_atomic_patch_happy_path() -> None:
    storage = _vfs({"foo.py": "def foo():\n    return 1\n"})
    out = await _patch_tool(storage)._arun(
        file_path="foo.py",
        search_block="def foo():\n    return 1",
        replace_block="def foo():\n    return 42",
    )
    assert "[atomic_code_patch] OK" in out
    assert "return 42" in storage["foo.py"]


@pytest.mark.anyio
async def test_atomic_patch_occ_mismatch_leaves_storage_unchanged() -> None:
    storage = _vfs({"foo.py": "def foo():\n    return 1\n"})
    before = dict(storage)
    out = await _patch_tool(storage)._arun(
        file_path="foo.py",
        search_block="def foo():\n    return 1",
        replace_block="def foo():\n    return 99",
        expected_hash="not-the-real-hash" * 4,
    )
    assert "OCC mismatch" in out
    assert storage == before  # byte-identical


@pytest.mark.anyio
async def test_atomic_patch_ast_validation_failure_leaves_storage_unchanged() -> None:
    """DoD MUST: a patch that breaks Python syntax must abort the write."""
    storage = _vfs({"foo.py": "def foo():\n    return 1\n"})
    before = dict(storage)
    # Replace with unclosed paren — ast.parse will raise SyntaxError.
    out = await _patch_tool(storage)._arun(
        file_path="foo.py",
        search_block="def foo():\n    return 1",
        replace_block="def foo():\n    return (",
    )
    assert "ERROR" in out
    assert "AST" in out or "SyntaxError" in out
    assert storage == before


@pytest.mark.anyio
async def test_atomic_patch_fuzzy_below_threshold_fails() -> None:
    storage = _vfs({"foo.py": "def foo():\n    return 1\n"})
    out = await _patch_tool(storage)._arun(
        file_path="foo.py",
        search_block="def totally_other_function():\n    raise RuntimeError",
        replace_block="def replaced():\n    return 0",
    )
    assert "ERROR" in out
    assert "fuzzy" in out or "not found" in out


@pytest.mark.anyio
async def test_atomic_patch_empty_replace_block_deletes() -> None:
    storage = _vfs(
        {"foo.py": "def foo():\n    return 1\n\ndef bar():\n    return 2\n"}
    )
    out = await _patch_tool(storage)._arun(
        file_path="foo.py",
        search_block="def foo():\n    return 1",
        replace_block="",
    )
    assert "OK" in out
    assert "def foo" not in storage["foo.py"]
    assert "def bar" in storage["foo.py"]  # other function survived


# =====================================================================
# BatchSemanticEditTool — Unit-of-Work / ACID guarantees
# =====================================================================


_A_INITIAL = "def alpha():\n    return 'A'\n"
_B_INITIAL = "def beta():\n    return 'B'\n"
_C_INITIAL = "def gamma():\n    return 'C'\n"


def _full_batch_storage() -> Dict[str, str]:
    return _vfs({"a.py": _A_INITIAL, "b.py": _B_INITIAL, "c.py": _C_INITIAL})


@pytest.mark.anyio
async def test_batch_happy_path_three_files() -> None:
    storage = _full_batch_storage()
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": _sha(_A_INITIAL),
                "search_block": "def alpha():\n    return 'A'",
                "replace_block": "def alpha():\n    return 'AA'",
            },
            {
                "file_path": "b.py",
                "document_version_id": _sha(_B_INITIAL),
                "search_block": "def beta():\n    return 'B'",
                "replace_block": "def beta():\n    return 'BB'",
            },
            {
                "file_path": "c.py",
                "document_version_id": _sha(_C_INITIAL),
                "search_block": "def gamma():\n    return 'C'",
                "replace_block": "def gamma():\n    return 'CC'",
            },
        ]
    )
    assert "OK" in out
    assert "return 'AA'" in storage["a.py"]
    assert "return 'BB'" in storage["b.py"]
    assert "return 'CC'" in storage["c.py"]


@pytest.mark.anyio
async def test_batch_occ_mismatch_rejects_whole_batch() -> None:
    """DoD MUST: a single stale document_version_id rejects the batch atomically."""
    storage = _full_batch_storage()
    before = dict(storage)
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": _sha(_A_INITIAL),
                "search_block": "def alpha():\n    return 'A'",
                "replace_block": "def alpha():\n    return 'AA'",
            },
            {
                "file_path": "b.py",
                "document_version_id": "stale-hash-of-b" * 4,  # wrong
                "search_block": "def beta():\n    return 'B'",
                "replace_block": "def beta():\n    return 'BB'",
            },
        ]
    )
    assert "OCC mismatch" in out
    assert "b.py" in out
    assert storage == before  # byte-identical — Phase 1 rejected before any write


@pytest.mark.anyio
async def test_batch_multiple_stale_items_all_reported() -> None:
    storage = _full_batch_storage()
    before = dict(storage)
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": "stale-1" * 8,
                "search_block": "def alpha():\n    return 'A'",
                "replace_block": "def alpha():\n    return 'AA'",
            },
            {
                "file_path": "b.py",
                "document_version_id": "stale-2" * 8,
                "search_block": "def beta():\n    return 'B'",
                "replace_block": "def beta():\n    return 'BB'",
            },
        ]
    )
    assert "OCC mismatch" in out
    assert "a.py" in out
    assert "b.py" in out
    assert storage == before


@pytest.mark.anyio
async def test_batch_ast_mid_failure_is_atomic() -> None:
    """ACID guarantee: an AST failure on item 2 must NOT commit item 1."""
    storage = _full_batch_storage()
    before = dict(storage)
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": _sha(_A_INITIAL),
                "search_block": "def alpha():\n    return 'A'",
                "replace_block": "def alpha():\n    return 'AA'",
            },
            {
                "file_path": "b.py",
                "document_version_id": _sha(_B_INITIAL),
                "search_block": "def beta():\n    return 'B'",
                "replace_block": "def beta():\n    return (",  # broken Python
            },
        ]
    )
    assert "ERROR on item 2" in out
    assert "No writes committed" in out
    # Critical: storage is byte-identical — item 1's buffered write was discarded.
    assert storage == before


@pytest.mark.anyio
async def test_batch_intra_batch_consistency_via_buffered_read() -> None:
    """Item 2 must see item 1's mutation via buffered_read."""
    storage = _vfs({"a.py": "x = 1\nMARKER_LINE = 'untouched'\n"})
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": _sha(storage["a.py"]),
                "search_block": "MARKER_LINE = 'untouched'",
                "replace_block": "MARKER_LINE = 'step_one_done'",
            },
            {
                "file_path": "a.py",
                # NOTE: this is intentionally the same pre-batch hash — Phase 1
                # validated it ONCE. Phase 2 suppresses inner OCC; item 2 matches
                # against item 1's buffered write via buffered_read.
                "document_version_id": _sha(storage["a.py"]),
                "search_block": "MARKER_LINE = 'step_one_done'",
                "replace_block": "MARKER_LINE = 'step_two_done'",
            },
        ]
    )
    assert "OK" in out
    assert storage["a.py"].endswith("MARKER_LINE = 'step_two_done'\n")


@pytest.mark.anyio
async def test_batch_empty_edits_returns_noop() -> None:
    storage = _full_batch_storage()
    before = dict(storage)
    out = await _batch_tool(storage)._arun(edits=[])
    assert "no items" in out
    assert storage == before


@pytest.mark.anyio
async def test_batch_fuzzy_failure_is_also_atomic() -> None:
    """A fuzzy-match failure mid-batch must also discard the buffer."""
    storage = _full_batch_storage()
    before = dict(storage)
    out = await _batch_tool(storage)._arun(
        edits=[
            {
                "file_path": "a.py",
                "document_version_id": _sha(_A_INITIAL),
                "search_block": "def alpha():\n    return 'A'",
                "replace_block": "def alpha():\n    return 'AA'",
            },
            {
                "file_path": "b.py",
                "document_version_id": _sha(_B_INITIAL),
                "search_block": "completely unrelated nonsense that won't fuzzy-match",
                "replace_block": "def nope():\n    return 0",
            },
        ]
    )
    assert "ERROR on item 2" in out
    assert storage == before


# =====================================================================
# FileWriteTool
# =====================================================================


@pytest.mark.anyio
async def test_file_write_create_new_file() -> None:
    storage: Dict[str, str] = {}
    out = await _write_tool(storage)._arun(
        file_path="new.py",
        content="def created():\n    return True\n",
    )
    assert "OK" in out
    assert storage["new.py"].startswith("def created()")


@pytest.mark.anyio
async def test_file_write_overwrite_with_matching_occ() -> None:
    storage = _vfs({"foo.py": "x = 1\n"})
    out = await _write_tool(storage)._arun(
        file_path="foo.py",
        content="x = 2\n",
        expected_hash=_sha("x = 1\n"),
    )
    assert "OK" in out
    assert storage["foo.py"] == "x = 2\n"


@pytest.mark.anyio
async def test_file_write_occ_mismatch_leaves_storage_unchanged() -> None:
    storage = _vfs({"foo.py": "x = 1\n"})
    before = dict(storage)
    out = await _write_tool(storage)._arun(
        file_path="foo.py",
        content="x = 999\n",
        expected_hash="not-the-real-hash" * 4,
    )
    assert "OCC mismatch" in out
    assert storage == before


@pytest.mark.anyio
async def test_file_write_python_ast_failure_leaves_storage_unchanged() -> None:
    storage: Dict[str, str] = {}
    out = await _write_tool(storage)._arun(
        file_path="broken.py",
        content="def broken(:\n    return\n",  # syntactically invalid
    )
    assert "ERROR" in out
    assert "broken.py" not in storage


# =====================================================================
# Task D — make_state_aware_read_file_tool
# =====================================================================


def test_state_aware_read_populates_read_files_state() -> None:
    state: Dict[str, Any] = {}
    tool = make_state_aware_read_file_tool(state, lambda _p: "content of x")
    out = tool.invoke({"path": "src/x.py"})
    assert "content of x" in out
    assert "read_files_state" in state
    assert "src/x.py" in state["read_files_state"]
    vfs_file = state["read_files_state"]["src/x.py"]
    assert vfs_file.blob_hash
    assert vfs_file.document_version_id
    assert vfs_file.is_dirty is False


def test_state_aware_read_overwrites_on_repeat_read() -> None:
    state: Dict[str, Any] = {}
    # First read returns content A.
    contents = {"src/x.py": "first-content"}
    tool = make_state_aware_read_file_tool(state, contents.get)
    tool.invoke({"path": "src/x.py"})
    first_hash = state["read_files_state"]["src/x.py"].blob_hash
    # Mutate the source between reads.
    contents["src/x.py"] = "second-content"
    tool.invoke({"path": "src/x.py"})
    second_hash = state["read_files_state"]["src/x.py"].blob_hash
    assert first_hash != second_hash


# =====================================================================
# register_mutation_tools
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


@pytest.mark.anyio
async def test_register_mutation_tools_registers_three_write_tier_schemas(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_mutation_tools(store)
    assert count == 3
    schemas = store.all_schemas()
    names = {s.name for s in schemas}
    assert names == {"atomic_code_patch", "batch_semantic_edit", "file_write"}


@pytest.mark.anyio
async def test_all_mutation_schemas_are_write_tier(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_mutation_tools(store)
    for schema in store.all_schemas():
        assert schema.privilege_tier is ToolPrivilegeTier.WRITE
        assert schema.allowed_roles == _ALLOWED_MUTATION_ROLES


# =====================================================================
# anyio backend constraint
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
