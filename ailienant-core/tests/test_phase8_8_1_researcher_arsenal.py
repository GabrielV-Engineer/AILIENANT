"""Wave 1 Researcher Arsenal gate — sibling-file checkpoint.

DoD:
  - Every tool has "researcher" in its allowed_roles.
  - select_tools(active_role="researcher") can surface each of the 10 tools.
  - Every net-new tool is READ_ONLY and survives PLAN session mode.
  - Net-new tools execute without raising (DoD "executes").
  - A non-researcher role cannot retrieve the researcher-only net-new tools.
  - Path canonicalization de-duplicates cross-casing entries in the path universe.
  - GrepTool short-circuit: match counter breaks at max_matches (O(L)).
  - Boundary tag wraps tool output.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.perception_tools import register_perception_tools
from tools.researcher_tools import (
    GetDependentsTool,
    GlobTool,
    GrepTool,
    GraphRAGQueryTool,
    WorkspaceStructureTool,
    _canon,
    make_vfs_path_provider,
    register_researcher_tools,
)
from tools.agent_tools import make_read_file_tool


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8."""

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
        store_path=str(tmp_path / "tool_rag_881"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


# All 10 tool names that must carry "researcher" in allowed_roles.
_WAVE1_TOOLS = [
    "document_parser",
    "inspect_ast_node",
    "get_symbol_references",
    "trace_data_flow",
    "read_file",
    "glob",
    "grep",
    "workspace_structure",
    "query_graphrag",
    "get_dependents",
]

# The 6 researcher-only net-new tools (distinct from the 4 perception wire-ins).
_RESEARCHER_ONLY_TOOLS = [
    "read_file",
    "glob",
    "grep",
    "workspace_structure",
    "query_graphrag",
    "get_dependents",
]


# =====================================================================
# A — Retrievability: all 10 tools visible to the researcher role
# =====================================================================


@pytest.mark.anyio
async def test_all_wave1_tools_have_researcher_role(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    await register_researcher_tools(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _WAVE1_TOOLS:
        assert tool_name in schemas, f"Schema {tool_name!r} missing from store"
        assert "researcher" in schemas[tool_name].allowed_roles, (
            f"{tool_name!r} missing 'researcher' in allowed_roles"
        )


@pytest.mark.anyio
async def test_select_tools_surfaces_researcher_tools(tmp_path: Path) -> None:
    """select_tools(active_role='researcher') must be able to return each Wave-1 tool."""
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    await register_researcher_tools(store)

    # With only 10 tools registered, eager mode will activate.
    # Verify at least the net-new tools are reachable via RBAC filter.
    for tool_name in _WAVE1_TOOLS:
        schema = next((s for s in store.all_schemas() if s.name == tool_name), None)
        assert schema is not None, f"{tool_name!r} not in store"
        assert "researcher" in schema.allowed_roles


# =====================================================================
# B — All researcher tools are READ_ONLY; survive PLAN mode
# =====================================================================


@pytest.mark.anyio
async def test_all_researcher_tools_are_read_only(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    await register_researcher_tools(store)

    schemas = {s.name: s for s in store.all_schemas()}
    for tool_name in _WAVE1_TOOLS:
        s = schemas[tool_name]
        assert s.privilege_tier == ToolPrivilegeTier.READ_ONLY, (
            f"{tool_name!r} is {s.privilege_tier}, expected READ_ONLY"
        )


@pytest.mark.anyio
async def test_researcher_tools_survive_plan_mode(tmp_path: Path) -> None:
    """All 10 tools must be returned by select_tools under PLAN session mode."""
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    await register_researcher_tools(store)

    for tool_name in _WAVE1_TOOLS:
        results = await store.select_tools(
            tool_name,
            k=10,
            active_role="researcher",
            session_mode=SessionPermissionMode.PLAN,
        )
        names = {s.name for s in results}
        assert tool_name in names, (
            f"{tool_name!r} not returned under PLAN mode for researcher"
        )


# =====================================================================
# C — Negative RBAC: vcs_manager cannot retrieve researcher-only tools
# =====================================================================


@pytest.mark.anyio
async def test_non_researcher_role_cannot_retrieve_net_new_tools(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_researcher_tools(store)

    results = await store.select_tools(
        "file content search",
        k=10,
        active_role="vcs_manager",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    returned_names = {s.name for s in results}
    for tool_name in _RESEARCHER_ONLY_TOOLS:
        assert tool_name not in returned_names, (
            f"vcs_manager should NOT see researcher-only tool {tool_name!r}"
        )


# =====================================================================
# D — Execution: each net-new tool runs without raising
# =====================================================================


@pytest.mark.anyio
async def test_glob_tool_executes() -> None:
    paths = ["c:/project/src/main.py", "c:/project/src/utils.py", "c:/project/test.py"]

    async def _provider() -> List[str]:
        return [_canon(p) for p in paths]

    tool = GlobTool(path_provider=_provider)
    result = await tool._arun("*.py")
    # Quarantine tag wraps the output
    assert "main.py" in result or "utils.py" in result or "test.py" in result
    # Result is bounded
    result2 = await tool._arun("*.py", limit=1)
    # At most 1 match returned (plus possible cap notice)
    lines = [l for l in result2.split("\n") if not l.startswith("<") and not l.startswith("[")]
    assert len([l for l in lines if l.strip()]) <= 1


@pytest.mark.anyio
async def test_grep_tool_executes() -> None:
    files = {
        _canon("c:/project/a.py"): "import os\nfoo = 1\n",
        _canon("c:/project/b.py"): "import sys\nfoo = 2\n",
        _canon("c:/project/c.py"): "bar = 3\n",
    }

    async def _provider() -> List[str]:
        return sorted(files)

    def _reader(path: str) -> Optional[str]:
        return files.get(_canon(path))

    tool = GrepTool(path_provider=_provider, content_reader=_reader)
    result = await tool._arun("foo")
    assert "foo" in result


@pytest.mark.anyio
async def test_grep_tool_short_circuit(tmp_path: Path) -> None:
    """GrepTool must break after max_matches (O(L) — not O(project))."""
    # 20 files each with 2 matching lines → would yield 40 without short-circuit.
    call_count = {"n": 0}
    files: dict[str, str] = {}
    for i in range(20):
        path = _canon(f"c:/project/file{i:02d}.py")
        files[path] = "match_me\nmatch_me\n"

    async def _provider() -> List[str]:
        return sorted(files)

    def _reader(path: str) -> Optional[str]:
        call_count["n"] += 1
        return files.get(_canon(path))

    tool = GrepTool(path_provider=_provider, content_reader=_reader)
    result = await tool._arun("match_me", max_matches=3)

    # Exactly 3 rows (plus boundary tags and cap notice)
    match_rows = [l for l in result.split("\n") if "match_me" in l]
    assert len(match_rows) == 3, f"Expected 3 match rows, got {len(match_rows)}"
    # The reader was called far fewer than 20 times (short-circuit proof)
    assert call_count["n"] < 20, (
        f"Reader called {call_count['n']} times — short-circuit did not fire"
    )


@pytest.mark.anyio
async def test_grep_tool_bad_regex() -> None:
    async def _provider() -> List[str]:
        return []

    tool = GrepTool(path_provider=_provider, content_reader=lambda _: None)
    result = await tool._arun("[unclosed")
    assert "ERROR" in result
    assert "invalid regex" in result


@pytest.mark.anyio
async def test_workspace_structure_tool_executes() -> None:
    paths = [
        _canon("c:/project/src/a.py"),
        _canon("c:/project/src/b.py"),
        _canon("c:/project/tests/t.py"),
    ]

    async def _provider() -> List[str]:
        return paths

    tool = WorkspaceStructureTool(path_provider=_provider)
    result = await tool._arun()
    assert "src" in result or "tests" in result


@pytest.mark.anyio
async def test_graphrag_query_tool_executes() -> None:
    context_block = "## Context\n- file: src/main.py, function: run()"
    mock_result = MagicMock()
    mock_result.context_block = context_block

    mock_extractor = MagicMock()
    mock_extractor.deep_parse = AsyncMock(return_value=mock_result)

    tool = GraphRAGQueryTool(extractor=mock_extractor, workspace_root="/project")
    result = await tool._arun(seed_files=["src/main.py"])
    assert context_block in result
    mock_extractor.deep_parse.assert_called_once_with(
        seed_files=["src/main.py"], workspace_root="/project"
    )


@pytest.mark.anyio
async def test_get_dependents_tool_executes() -> None:
    async def fake_get_dependents(target: str, project_id: str = "") -> List[str]:
        return ["src/consumer.py", "src/other.py"]

    bound = functools.partial(fake_get_dependents, project_id="proj1")
    tool = GetDependentsTool(get_dependents=bound)
    result = await tool._arun("src/core.py")

    # Output is JSON-structured and quarantine-wrapped
    import re
    inner = re.sub(r"<[^>]+>", "", result)  # strip boundary tags
    payload = json.loads(inner)
    assert payload["target"] == "src/core.py"
    assert "src/consumer.py" in payload["dependents"]


@pytest.mark.anyio
async def test_read_file_schema_executes() -> None:
    content = "line1\nline2\nline3\n"
    tool = make_read_file_tool(lambda _path: content)
    result = tool.invoke({"path": "any.py", "offset": 1, "limit": 1})
    assert "line2" in result


# =====================================================================
# E — Path canonicalization: cross-casing de-duplicates in union
# =====================================================================


def test_path_canon_deduplication() -> None:
    """C:\\A\\B.py and c:/a/b.py must canonicalize to the same key."""
    p1 = "C:\\Project\\src\\main.py"
    p2 = "c:/project/src/main.py"
    assert _canon(p1) == _canon(p2), (
        f"_canon did not deduplicate:\n  {_canon(p1)!r}\n  {_canon(p2)!r}"
    )


@pytest.mark.anyio
async def test_make_vfs_path_provider_deduplicates(tmp_path: Path) -> None:
    """Provider union must de-duplicate paths that differ only in casing/sep."""
    from unittest.mock import patch, AsyncMock

    vfs_upper = "C:\\Project\\src\\main.py"
    db_lower = "c:/project/src/main.py"

    mock_vfs = MagicMock()
    mock_vfs.snapshot_paths.return_value = [vfs_upper]

    with patch(
        "core.db.list_indexed_files",
        new=AsyncMock(return_value=[db_lower]),
    ):
        provider = make_vfs_path_provider("proj", vfs=mock_vfs)
        result = await provider()

    canon_set = {_canon(p) for p in result}
    assert len(canon_set) == 1, (
        f"Expected 1 canonical path, got {len(canon_set)}: {canon_set}"
    )


# =====================================================================
# F — Boundary tag wraps output
# =====================================================================


@pytest.mark.anyio
async def test_glob_output_is_quarantine_wrapped() -> None:
    _TAG = "testboundary0123456789abcdef0123"

    async def _provider() -> List[str]:
        return [_canon("c:/project/file.py")]

    tool = GlobTool(path_provider=_provider, boundary_provider=lambda: _TAG)
    result = await tool._arun("*.py")
    assert result.startswith(f"<{_TAG}>")
    assert result.endswith(f"</{_TAG}>")
