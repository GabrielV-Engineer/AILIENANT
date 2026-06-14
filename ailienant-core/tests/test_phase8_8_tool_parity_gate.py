# ailienant-core/tests/test_phase8_8_tool_parity_gate.py
#
# Division checkpoint gate for the Tool Parity Matrix. Composes the WHOLE agent arsenal
# into one isolated store and proves the cross-cutting invariants no single wave test can:
#
#   R1a — integrity: every registered schema resolves through ToolRAGStore (valid tier,
#         parseable json_schema, non-empty allowed_roles). Iterating all_schemas() — NOT
#         summing register_* returns — because Wave 4 formalizes names that other modules
#         also register, so idempotent upsert legitimately dedupes the catalog.
#   R1b — retrievability: a sampled READ_ONLY tool per concern is retrievable by its own
#         description query under an allowed role (zero-distance rank-0 under the fake
#         embedding; READ_ONLY so the read-only-survivor swap can never displace it).
#   R2  — RBAC enforcement: a role outside a tool's allowed_roles never holds it (asserted
#         on the authoritative allowed_roles membership, immune to vector-ranking noise).
#   R3  — Wave-0 reduction floor: against the cold-prompt worst case (the ENTIRE registered
#         catalog), a TOP_K selection respects TOOL_RAG_MIN_REDUCTION — the literal "the
#         matrix never blows the prompt budget" guarantee. (Per-role eager baselines are
#         already RBAC-narrowed, so they understate the catalog-level reduction.)
#   R4  — ISO: agents/roles.py role contracts did not degrade under the parity work.

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Dict, List

import pytest

from agents.roles import ROLE_REGISTRY
from core.deferred_tool_loader import DeferredToolLoader
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import TOOL_RAG_MIN_REDUCTION, ToolRAGStore, ToolSchema
from tools.analyst_tools import register_analyst_tools
from tools.coder_tools import register_coder_tools
from tools.control_tools import register_control_tools
from tools.execution_tools import register_execution_tools
from tools.gateway_tools import register_gateway_tools
from tools.meta_tools import register_meta_tools
from tools.mutation_tools import register_mutation_tools
from tools.orchestrator_tools import register_orchestrator_tools
from tools.perception_tools import register_perception_tools
from tools.planner_tools import register_planner_tools
from tools.researcher_tools import register_researcher_tools
from tools.universal_tools import register_universal_tools


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# Fixtures — isolated store + full-catalog registration
# =====================================================================


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
        store_path=str(tmp_path / "tool_rag_gate"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


async def _register_all(store: ToolRAGStore) -> None:
    """Seed the entire agent arsenal. Every register_*_tools is schema-only and takes just
    the store (tool-instance dependency injection happens at instantiation, not registration)."""
    await register_meta_tools(store)
    await register_control_tools(store)
    await register_researcher_tools(store)
    await register_analyst_tools(store)
    await register_orchestrator_tools(store)
    await register_planner_tools(store)
    await register_coder_tools(store)
    await register_execution_tools(store)
    await register_mutation_tools(store)
    await register_perception_tools(store)
    await register_gateway_tools(store)
    await register_universal_tools(store)


def _by_name(store: ToolRAGStore) -> Dict[str, ToolSchema]:
    return {s.name: s for s in store.all_schemas()}


# =====================================================================
# R1a — integrity: every registered schema resolves through ToolRAGStore
# =====================================================================


@pytest.mark.anyio
async def test_every_schema_resolves_and_is_well_formed(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    schemas = store.all_schemas()
    assert len(schemas) >= 40, "catalog smaller than the parity matrix expects"

    for s in schemas:
        assert s.name, "schema with empty name"
        assert isinstance(s.privilege_tier, ToolPrivilegeTier)
        parsed = json.loads(s.json_schema)  # raises if malformed → resolution failure
        assert isinstance(parsed, dict)
        assert s.allowed_roles, f"{s.name} registered with empty allowed_roles"


# =====================================================================
# R1b — retrievability: sampled READ_ONLY tools surface by description query
# =====================================================================


@pytest.mark.anyio
async def test_sampled_read_only_tools_are_retrievable(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    by_name = _by_name(store)

    # (tool_name, an allowed role). All READ_ONLY so the read-only-survivor swap is a no-op.
    samples = [
        ("todo_write", "planner"),
        ("tool_search", "researcher"),
        ("get_wbs_status", "orchestrator"),
    ]
    for name, role in samples:
        schema = by_name.get(name)
        assert schema is not None, f"{name} absent from the catalog"
        assert schema.privilege_tier is ToolPrivilegeTier.READ_ONLY
        assert role in schema.allowed_roles
        matches = await store.select_tools(
            schema.description,  # exact description → zero-distance rank-0 hit
            active_role=role,
            session_mode=SessionPermissionMode.DEFAULT,
        )
        assert name in {m.name for m in matches}, f"{name} not retrievable for {role!r}"


# =====================================================================
# R2 — RBAC enforcement: out-of-set roles never hold the tool
# =====================================================================


@pytest.mark.anyio
async def test_rbac_excludes_out_of_set_roles(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    by_name = _by_name(store)

    # (tool, role that must NOT hold it). Asserted on the authoritative allowed_roles set so a
    # vector-ranking miss can never produce a false pass.
    forbidden = [
        ("file_write", "researcher"),
        ("run_benchmark", "planner"),
        ("task_list", "qa_tester"),
        ("run_benchmark", "core_dev"),
    ]
    for name, role in forbidden:
        schema = by_name.get(name)
        if schema is None:
            pytest.fail(f"expected tool {name!r} not registered")
        assert role not in schema.allowed_roles, f"{role!r} unexpectedly holds {name!r}"

        # And it must never surface via selection for that role, in any mode.
        for mode in (SessionPermissionMode.DEFAULT, SessionPermissionMode.AUTO):
            matches = await store.select_tools(
                schema.description, active_role=role, session_mode=mode
            )
            assert name not in {m.name for m in matches}


# =====================================================================
# R3 — Wave-0 reduction floor holds at real-catalog scale
# =====================================================================


@pytest.mark.anyio
async def test_real_catalog_respects_reduction_floor(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)

    # Cold-prompt worst case: a naive agent injects the ENTIRE catalog. Tool RAG replaces that
    # with a TOP_K selection — the reduction this buys is the budget guarantee.
    eager = store.all_schemas()
    selected = await store.select_tools(
        "apply a patch to the file",
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    metrics = ToolRAGStore.prompt_size_metrics(eager, selected)
    assert metrics["reduction_ratio"] >= TOOL_RAG_MIN_REDUCTION, (
        f"reduction_ratio={metrics['reduction_ratio']:.3f} below "
        f"TOOL_RAG_MIN_REDUCTION={TOOL_RAG_MIN_REDUCTION}"
    )

    # The deferred mechanism must also engage at a realistically small context window.
    loader = DeferredToolLoader(store)
    decision = await loader.resolve(
        "apply a patch to the file",
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
        context_window=8192,
    )
    assert decision.mode == "deferred"


# =====================================================================
# R4 — ISO: agents/roles.py role contracts did not degrade
# =====================================================================

# Frozen snapshot of each canonical role's tool whitelist. The parity work wires
# `allowed_roles` on tool SCHEMAS; it must never mutate the role CONTRACTS here. A diff in
# either direction (tool added or removed) is a deliberate decision that must update this snapshot.
_FROZEN_ROLE_TOOLS: Dict[str, frozenset] = {
    "core_dev": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "WriteFileTool", "RunLinterTool", "pytest", "DocumentParserTool",
    }),
    "architect_refactor": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "BatchEditTool", "RunLinterTool", "pytest", "DocumentParserTool",
    }),
    "devops_infra": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "WriteFileTool", "BashTool", "RunLinterTool", "pytest", "DocumentParserTool",
    }),
    "secops": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "RunLinterTool", "pytest", "DocumentParserTool",
    }),
    "qa_tester": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "BashTool", "RunLinterTool", "pytest", "DocumentParserTool",
    }),
    "doc_manager": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "WriteFileTool", "DocumentParserTool",
    }),
    "vcs_manager": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "BashTool",
        "DocumentParserTool",
    }),
    "data_ml_engineer": frozenset({
        "FileReadTool", "GrepTool", "GlobTool", "query_graphrag", "apply_patch",
        "WriteFileTool", "BashTool", "RunLinterTool", "pytest", "DocumentParserTool",
    }),
}

_REQUIRED_CONFIG_KEYS = frozenset(
    {"system_prompt", "allowed_tools", "forbidden_phrases", "hitl_triggers"}
)


def test_role_registry_contracts_unchanged() -> None:
    # Exactly the 8 canonical roles — no silent addition or removal.
    assert set(ROLE_REGISTRY) == set(_FROZEN_ROLE_TOOLS)

    for role, config in ROLE_REGISTRY.items():
        assert _REQUIRED_CONFIG_KEYS <= set(config), f"{role} lost a contract key"
        assert config["system_prompt"].strip(), f"{role} has an empty directive"
        assert frozenset(config["allowed_tools"]) == _FROZEN_ROLE_TOOLS[role], (
            f"{role} tool whitelist degraded"
        )
