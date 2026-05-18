# ailienant-core/tests/test_tool_rag_selection.py
#
# Phase 5.2 smoke tests for the Tool RAG selection layer.
# Style mirrors test_routing.py + test_fast_boot.py: parametrize + AsyncMock,
# no global fixtures, deterministic fake embeddings.
#
# DoD: pytest ailienant-core/tests/test_tool_rag_selection.py -v exit 0.

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Awaitable, Callable, List

import pytest

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import (
    TOOL_RAG_TOP_K,
    ToolRAGStore,
    ToolSchema,
)

# ---------------------------------------------------------------------------
# Test infrastructure: deterministic fake embed_fn
# ---------------------------------------------------------------------------

_EMBED_DIM = 8


def _fake_embed_factory(dim: int = _EMBED_DIM) -> Callable[[str], Awaitable[List[float]]]:
    """Build a deterministic embed_fn: sha256(text) → first `dim` float32 values."""

    async def _embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(dim):
            chunk = digest[(i * 4) % len(digest):(i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            # litellm-style normalized magnitudes are usually small; clamp.
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return _embed


def _make_store(tmp_path: Path) -> ToolRAGStore:
    return ToolRAGStore(
        embed_fn=_fake_embed_factory(),
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=_EMBED_DIM,
        register_atexit_cleanup=False,
    )


def _schema(
    name: str,
    description: str,
    tier: ToolPrivilegeTier,
    roles: List[str],
    json_schema: str = '{"type":"object"}',
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json_schema,
        privilege_tier=tier,
        allowed_roles=frozenset(roles),
    )


async def _seed_basic_catalog(store: ToolRAGStore) -> None:
    """Register a small, varied catalog used by multiple tests."""
    await store.register_schema(
        _schema("FileReadTool", "Read file contents by path", ToolPrivilegeTier.READ_ONLY, ["core_dev", "qa_tester"])
    )
    await store.register_schema(
        _schema("FileWriteTool", "Write content to a file", ToolPrivilegeTier.WRITE, ["core_dev"])
    )
    await store.register_schema(
        _schema("SandboxBashTool", "Run a shell command in a sandbox", ToolPrivilegeTier.EXECUTE, ["core_dev"])
    )
    await store.register_schema(
        _schema("RmRfTool", "Recursive force delete a directory", ToolPrivilegeTier.DANGEROUS, ["core_dev"])
    )
    await store.register_schema(
        _schema("InspectAstTool", "Inspect AST nodes for a source file", ToolPrivilegeTier.READ_ONLY, ["core_dev"])
    )
    await store.register_schema(
        _schema("RunPytest", "Run the pytest suite", ToolPrivilegeTier.EXECUTE, ["qa_tester"])
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_top_k_cap(tmp_path: Path) -> None:
    """Registering more than k tools must still cap selection at k."""
    store = _make_store(tmp_path)
    for i in range(12):
        await store.register_schema(
            _schema(f"Tool{i}", f"tool number {i}", ToolPrivilegeTier.READ_ONLY, ["core_dev"])
        )
    selected = await store.select_tools(
        "do something",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert len(selected) <= TOOL_RAG_TOP_K


@pytest.mark.anyio
async def test_read_only_always_present_when_available(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await _seed_basic_catalog(store)
    selected = await store.select_tools(
        "edit configuration files",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert any(
        s.privilege_tier is ToolPrivilegeTier.READ_ONLY for s in selected
    ), f"Expected at least one READ_ONLY tool, got: {[s.name for s in selected]}"


@pytest.mark.anyio
async def test_plan_session_excludes_non_read_only(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await _seed_basic_catalog(store)
    selected = await store.select_tools(
        "anything goes",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.PLAN,
    )
    assert selected, "PLAN session must still surface READ_ONLY tools"
    assert all(
        s.privilege_tier is ToolPrivilegeTier.READ_ONLY for s in selected
    ), f"PLAN must filter to READ_ONLY only, got: {[(s.name, s.privilege_tier.value) for s in selected]}"


@pytest.mark.anyio
async def test_rbac_filters_by_role(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await _seed_basic_catalog(store)
    # qa_tester only sees FileReadTool + RunPytest from the basic catalog.
    selected = await store.select_tools(
        "run the test suite",
        k=TOOL_RAG_TOP_K,
        active_role="qa_tester",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    names = {s.name for s in selected}
    assert names <= {"FileReadTool", "RunPytest"}, f"Leaked outside qa_tester whitelist: {names}"


@pytest.mark.anyio
async def test_role_with_no_matching_schemas_returns_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await _seed_basic_catalog(store)
    selected = await store.select_tools(
        "anything",
        k=TOOL_RAG_TOP_K,
        active_role="doc_manager",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert selected == []


@pytest.mark.anyio
async def test_determinism_on_repeated_calls(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await _seed_basic_catalog(store)
    first = await store.select_tools(
        "refactor the parser",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    second = await store.select_tools(
        "refactor the parser",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    third = await store.select_tools(
        "refactor the parser",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert [s.name for s in first] == [s.name for s in second] == [s.name for s in third]


@pytest.mark.anyio
async def test_empty_store_returns_empty_list(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    selected = await store.select_tools(
        "any intent",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    assert selected == []


@pytest.mark.anyio
async def test_idempotent_register_schema(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.register_schema(
        _schema("DupTool", "first description", ToolPrivilegeTier.READ_ONLY, ["core_dev"])
    )
    await store.register_schema(
        _schema("DupTool", "second description", ToolPrivilegeTier.READ_ONLY, ["core_dev"])
    )
    schemas = [s for s in store.all_schemas() if s.name == "DupTool"]
    assert len(schemas) == 1
    assert schemas[0].description == "second description"


@pytest.mark.anyio
async def test_register_schema_rejects_wrong_dim(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    bad = ToolSchema(
        name="BadDim",
        description="x",
        json_schema="{}",
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=frozenset({"core_dev"}),
        embedding=[0.1, 0.2],  # wrong dim
    )
    with pytest.raises(ValueError, match="embedding dim mismatch"):
        await store.register_schema(bad)


def test_prompt_size_metrics_shape() -> None:
    eager = [
        ToolSchema("a", "", "x" * 100, ToolPrivilegeTier.READ_ONLY, frozenset()),
        ToolSchema("b", "", "y" * 100, ToolPrivilegeTier.WRITE, frozenset()),
    ]
    selected = [eager[0]]
    metrics = ToolRAGStore.prompt_size_metrics(eager, selected)
    assert set(metrics.keys()) == {"eager_size", "selected_size", "reduction_ratio"}
    assert metrics["eager_size"] == 200.0
    assert metrics["selected_size"] == 100.0
    assert metrics["reduction_ratio"] == pytest.approx(0.5)


def test_prompt_size_metrics_zero_eager_returns_zero_reduction() -> None:
    metrics = ToolRAGStore.prompt_size_metrics([], [])
    assert metrics["reduction_ratio"] == 0.0


# ---------------------------------------------------------------------------
# anyio backend constraint — match the project's existing async-test setup
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
