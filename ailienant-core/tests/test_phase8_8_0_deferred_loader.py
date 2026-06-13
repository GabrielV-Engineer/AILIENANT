# ailienant-core/tests/test_phase8_8_0_deferred_loader.py
#
# Wave 0 infra gate — DeferredToolLoader + ToolSearchTool.
#
# Proves the eager-vs-deferred policy holds at the division's 56-schema scale
# WITHOUT registering 42 phantom production tools: the catalog here is a
# test-only synthetic fixture in an isolated store. The real 56-tool catalog is
# asserted wave-by-wave and at the division checkpoint gate.
#
#   A   — mode switches: 56 schemas + small window -> deferred (<= TOP_K);
#         huge window -> eager (whole visible catalog, > TOP_K).
#   B   — deferred reduction_ratio >= TOOL_RAG_MIN_REDUCTION.
#   C   — tool_search retrieves the needle by query (ContextVar fallback path)
#         and returns the shift-left instruction, not bare JSON.
#   C2  — config-threaded role wins over a divergent ambient ContextVar role.
#   D   — tool_search is always present in a deferred set, bound stays <= TOP_K.
#   E   — tool_search invariants: READ_ONLY, all-roles, survives PLAN mode.
#   F   — k=1 deferred returns exactly [tool_search] (no select_tools(k=0) leak).

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Dict, List

import pytest

from core.deferred_tool_loader import DeferredToolLoader
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import TOOL_RAG_MIN_REDUCTION, TOOL_RAG_TOP_K, ToolRAGStore, ToolSchema
from tools.control_tools import _CONTROL_ROLES
from tools.meta_tools import ToolSearchTool, register_meta_tools

# Needle: distinctively described so an exact-text query lands a zero-distance
# hit under the deterministic fake embedding (which has no semantic similarity).
_NEEDLE_NAME = "synthetic_db_migrate_runner"
_NEEDLE_DESC = "Run idempotent database schema migrations against the staging replica."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# Fixtures — isolated store + synthetic 56-schema catalog
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """SHA256-based deterministic fake embeddings (no network). dim=8."""

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


def _padded_schema_json(i: int) -> str:
    """A realistic ~220-char JSON schema so the 56-set exceeds the char threshold."""
    return json.dumps(
        {
            "type": "object",
            "properties": {
                "arg": {
                    "type": "string",
                    "description": f"Decoy parameter for synthetic tool {i}. "
                    + ("padding " * 12),
                }
            },
            "required": ["arg"],
        }
    )


async def _seed_synthetic_catalog(store: ToolRAGStore, n: int = 56) -> None:
    """Register n decoys (all visible to core_dev+qa_tester) + the needle + tool_search."""
    tiers = [
        ToolPrivilegeTier.READ_ONLY,
        ToolPrivilegeTier.WRITE,
        ToolPrivilegeTier.EXECUTE,
        ToolPrivilegeTier.READ_ONLY,
    ]
    for i in range(n - 1):  # n-1 decoys; the needle is the nth
        await store.register_schema(
            ToolSchema(
                name=f"synthetic_tool_{i:02d}",
                description=f"Synthetic decoy capability number {i}.",
                json_schema=_padded_schema_json(i),
                privilege_tier=tiers[i % len(tiers)],
                allowed_roles=frozenset({"core_dev", "qa_tester"}),
            )
        )
    # Needle — visible to core_dev ONLY (lets C2 prove the config role won).
    await store.register_schema(
        ToolSchema(
            name=_NEEDLE_NAME,
            description=_NEEDLE_DESC,
            json_schema=_padded_schema_json(999),
            privilege_tier=ToolPrivilegeTier.READ_ONLY,
            allowed_roles=frozenset({"core_dev"}),
        )
    )
    await register_meta_tools(store)


# =====================================================================
# A — mode switch
# =====================================================================


@pytest.mark.anyio
async def test_mode_switches_with_context_budget(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    loader = DeferredToolLoader(store)

    # Small window -> the 56-schema payload exceeds ~10% -> deferred, capped.
    deferred = await loader.resolve(
        "anything", active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT, context_window=8192,
    )
    assert deferred.mode == "deferred"
    assert len(deferred.schemas) <= TOOL_RAG_TOP_K

    # Huge window -> the whole visible catalog fits -> eager (NOT capped).
    eager = await loader.resolve(
        "anything", active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT, context_window=10_000_000,
    )
    assert eager.mode == "eager"
    assert len(eager.schemas) == eager.eager_count > TOOL_RAG_TOP_K
    assert eager.reduction_ratio == 0.0  # nothing dropped


# =====================================================================
# B — deferred reduction floor (the financial DoD at 56 schemas)
# =====================================================================


@pytest.mark.anyio
async def test_deferred_reduction_meets_floor(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    loader = DeferredToolLoader(store)

    decision = await loader.resolve(
        "run the migration", active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT, context_window=8192,
    )
    assert decision.mode == "deferred"
    assert decision.reduction_ratio >= TOOL_RAG_MIN_REDUCTION, (
        f"reduction_ratio={decision.reduction_ratio:.3f} below "
        f"TOOL_RAG_MIN_REDUCTION={TOOL_RAG_MIN_REDUCTION}"
    )


# =====================================================================
# C — retrievability by query (ContextVar fallback) + discovery semantics
# =====================================================================


@pytest.mark.anyio
async def test_tool_search_retrieves_needle_via_contextvar(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    tool = ToolSearchTool(store=store)

    from tools.mcp_adapter import _task_active_role

    token = _task_active_role.set("core_dev")
    try:
        # Exact needle description => zero-distance hit under the fake embedding.
        result = await tool._arun(query=_NEEDLE_DESC, k=5, config=None)
    finally:
        _task_active_role.reset(token)

    assert _NEEDLE_NAME in result
    # Discovery, not direct-load: instruction must be present, full schema absent.
    assert "name it explicitly" in result
    assert '"properties"' not in result  # no full json_schema round-trips


# =====================================================================
# C2 — config-threaded role wins over a divergent ambient ContextVar role
# =====================================================================


@pytest.mark.anyio
async def test_config_role_overrides_stale_contextvar(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    tool = ToolSearchTool(store=store)

    from tools.mcp_adapter import _task_active_role

    # Ambient role is qa_tester (cannot see the core_dev-only needle); the live
    # config role is core_dev (can). If config wins, the needle surfaces.
    token = _task_active_role.set("qa_tester")
    try:
        result = await tool._arun(
            query=_NEEDLE_DESC,
            k=5,
            config={"configurable": {"active_role": "core_dev"}},
        )
    finally:
        _task_active_role.reset(token)

    assert _NEEDLE_NAME in result, "config role did not win over stale ContextVar"


# =====================================================================
# D — tool_search always present in a deferred set; bound <= TOP_K
# =====================================================================


@pytest.mark.anyio
async def test_tool_search_always_in_deferred_set(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    loader = DeferredToolLoader(store)

    decision = await loader.resolve(
        "synthetic decoy capability number 3", active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT, context_window=8192,
    )
    assert decision.mode == "deferred"
    names = {s.name for s in decision.schemas}
    assert "tool_search" in names
    assert len(decision.schemas) <= TOOL_RAG_TOP_K


# =====================================================================
# E — tool_search invariants
# =====================================================================


@pytest.mark.anyio
async def test_tool_search_invariants(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)

    by_name: Dict[str, ToolSchema] = {s.name: s for s in store.all_schemas()}
    ts = by_name["tool_search"]
    assert ts.privilege_tier is ToolPrivilegeTier.READ_ONLY
    assert ts.allowed_roles == _CONTROL_ROLES

    # Survives PLAN mode (READ_ONLY-only filter) — still in the deferred set.
    loader = DeferredToolLoader(store)
    decision = await loader.resolve(
        "anything", active_role="core_dev",
        session_mode=SessionPermissionMode.PLAN, context_window=8192,
    )
    assert decision.mode == "deferred"
    assert "tool_search" in {s.name for s in decision.schemas}


# =====================================================================
# F — k=1 deferred returns exactly [tool_search] (no select_tools(k=0) leak)
# =====================================================================


@pytest.mark.anyio
async def test_k1_deferred_returns_only_tool_search(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _seed_synthetic_catalog(store, n=56)
    loader = DeferredToolLoader(store)

    decision = await loader.resolve(
        "anything", active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT, context_window=8192, k=1,
    )
    assert decision.mode == "deferred"
    assert [s.name for s in decision.schemas] == ["tool_search"]
