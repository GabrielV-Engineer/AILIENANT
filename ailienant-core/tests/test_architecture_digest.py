"""Architecture-overview digest: bounded synthesis of persisted graph analytics.

Exercises ``build_architecture_digest_sync`` directly against seeded, already-
relativized tuples (no live DB), plus the tool layer and dual-agent wiring:
- languages by extension, top-level module grouping (incl. the flat-repo ``<root>``
  fold), centrality hotspots with a deterministic score tie-break, community
  cluster sizing, and entrypoint detection that excludes test files,
- per-section caps with a truthful ``total`` (bounded/paginated), and an
  empty/cold project returning the well-formed skeleton (no ``KeyError``),
- the token backstop trims deterministically and in a bounded number of passes,
- the ArchitectureDigestTool returns JSON on success and a well-formed skeleton
  on failure, and is reachable (executable + registered) for BOTH the analyst and
  researcher.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from brain.memory import (
    _CLUSTER_LIMIT,
    _ENTRYPOINT_LIMIT,
    _MODULE_LIMIT,
    _ROOT_MODULE_LABEL,
    _empty_digest,
    build_architecture_digest_sync,
)
from core.tool_rag import ToolRAGStore
from tools.perception_tools import (
    _DIGEST_TOKEN_CAP,
    _DIGEST_TRIM_ORDER,
    ArchitectureDigestTool,
    _enforce_digest_token_cap,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8 (mirrors 8.8.2)."""

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
        store_path=str(tmp_path / "tool_rag_arch_digest"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


def _digest(
    *,
    rel_files: Tuple[str, ...] = (),
    top_ppr_rel: Tuple[Tuple[str, float], ...] = (),
    community_ids: Tuple[int, ...] = (),
    edge_count: int = 0,
) -> Dict[str, Any]:
    return build_architecture_digest_sync(
        rel_files=rel_files,
        top_ppr_rel=top_ppr_rel,
        community_ids=community_ids,
        edge_count=edge_count,
    )


# ── Pure assembler ────────────────────────────────────────────────────────────


def test_languages_counted_and_sorted() -> None:
    d = _digest(rel_files=("a.py", "core/b.py", "c.py", "readme.md"))
    assert d["languages"]["top"] == [
        {"language": ".py", "count": 3},
        {"language": ".md", "count": 1},
    ]
    assert d["languages"]["total"] == 2


def test_top_modules_group_by_first_segment() -> None:
    d = _digest(rel_files=("core/db.py", "core/x.py", "brain/y.py"))
    assert d["top_modules"]["top"][0] == {"module": "core", "count": 2}
    assert {"module": "brain", "count": 1} in d["top_modules"]["top"]


def test_flat_repo_files_fold_into_root_label() -> None:
    d = _digest(rel_files=("main.py", "utils.py", "config.py"))
    assert d["top_modules"]["top"] == [{"module": _ROOT_MODULE_LABEL, "count": 3}]


def test_hotspots_sorted_with_deterministic_tie_break() -> None:
    # Equal scores must fall back to path order so the LIMIT/ranking is stable.
    d = _digest(top_ppr_rel=(("b.py", 0.5), ("a.py", 0.5), ("c.py", 0.9)))
    assert [h["file"] for h in d["hotspots"]["top"]] == ["c.py", "a.py", "b.py"]


def test_community_clusters_sized_largest_first() -> None:
    d = _digest(community_ids=(0, 0, 0, 1, 1, 2))
    assert d["community_clusters"]["count"] == 3
    assert d["community_clusters"]["largest"] == [
        {"id": 0, "size": 3},
        {"id": 1, "size": 2},
        {"id": 2, "size": 1},
    ]


def test_entrypoints_include_app_files_exclude_tests() -> None:
    d = _digest(
        rel_files=(
            "main.py",
            "app.py",
            "tests/test_foo.py",
            "conftest.py",
            "pkg/util.py",
        )
    )
    assert d["entrypoints"]["top"] == ["app.py", "main.py"]
    assert d["entrypoints"]["total"] == 2


def test_graph_schema_counts() -> None:
    d = _digest(rel_files=("a.py", "b.py"), edge_count=7)
    assert d["graph_schema"] == {"indexed_files": 2, "edges": 7}


# ── Bounded / paginated ───────────────────────────────────────────────────────


def test_module_section_capped_with_truthful_total() -> None:
    files = tuple(f"mod{i:03d}/f.py" for i in range(_MODULE_LIMIT + 5))
    d = _digest(rel_files=files)
    assert len(d["top_modules"]["top"]) == _MODULE_LIMIT
    assert d["top_modules"]["total"] == _MODULE_LIMIT + 5


def test_cluster_and_entrypoint_caps_enforced() -> None:
    community_ids = tuple(range(_CLUSTER_LIMIT + 3))
    entries = tuple(f"svc{i:03d}/main.py" for i in range(_ENTRYPOINT_LIMIT + 4))
    d = _digest(rel_files=entries, community_ids=community_ids)
    assert len(d["community_clusters"]["largest"]) == _CLUSTER_LIMIT
    assert len(d["entrypoints"]["top"]) == _ENTRYPOINT_LIMIT
    assert d["entrypoints"]["total"] == _ENTRYPOINT_LIMIT + 4


# ── Empty / skeleton shape ────────────────────────────────────────────────────


def test_empty_input_returns_well_formed_skeleton() -> None:
    assert _digest() == _empty_digest()


def test_skeleton_has_every_key_typed() -> None:
    sk = _empty_digest()
    assert sk["languages"] == {"total": 0, "top": []}
    assert sk["community_clusters"] == {"count": 0, "largest": []}
    assert sk["graph_schema"] == {"indexed_files": 0, "edges": 0}
    # A consumer can index every section unconditionally.
    for key in ("languages", "top_modules", "hotspots", "entrypoints"):
        section = sk[key]
        assert isinstance(section, dict)
        assert "top" in section


# ── Determinism ───────────────────────────────────────────────────────────────


def test_output_is_deterministic() -> None:
    def run() -> Dict[str, Any]:
        return _digest(
            rel_files=("core/a.py", "core/b.py", "brain/c.py", "main.py"),
            top_ppr_rel=(("core/a.py", 0.5), ("core/b.py", 0.5)),
            community_ids=(0, 0, 1),
            edge_count=3,
        )

    assert json.dumps(run()) == json.dumps(run())


# ── Token backstop ────────────────────────────────────────────────────────────


def _oversized_digest():
    long = "x" * 400
    return {
        "languages": {"total": 30, "top": [{"language": long, "count": 1}] * 30},
        "top_modules": {"total": 30, "top": [{"module": long, "count": 1}] * 30},
        "hotspots": {"total": 30, "top": [{"file": long, "score": 0.1}] * 30},
        "community_clusters": {"count": 5, "largest": [{"id": 1, "size": 1}] * 30},
        "entrypoints": {"total": 30, "top": [long] * 30},
        "graph_schema": {"indexed_files": 30, "edges": 99},
    }


def test_token_cap_enforced() -> None:
    from tools.token_counter import PrecisionTokenCounter

    trimmed = _enforce_digest_token_cap(_oversized_digest())
    assert PrecisionTokenCounter.count(json.dumps(trimmed)) <= _DIGEST_TOKEN_CAP


def test_token_trim_is_bounded_not_quadratic(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.token_counter import PrecisionTokenCounter

    real = PrecisionTokenCounter.count
    calls = {"n": 0}

    def counting(text: str) -> int:
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(PrecisionTokenCounter, "count", staticmethod(counting))
    _enforce_digest_token_cap(_oversized_digest())
    # One initial count + at most one per section dropped — never per-element.
    assert calls["n"] <= len(_DIGEST_TRIM_ORDER) + 1


# ── Tool layer ────────────────────────────────────────────────────────────────


async def test_tool_returns_digest_json_on_success() -> None:
    tool = ArchitectureDigestTool(project_id="proj", workspace_root="/ws")
    with (
        patch("core.db.list_indexed_files", new=AsyncMock(return_value=["/ws/core/db.py", "/ws/main.py"])),
        patch("core.db.get_top_ppr_files", new=AsyncMock(return_value=[("/ws/core/db.py", 0.9)])),
        patch("core.db.get_all_community_ids", new=AsyncMock(return_value={"/ws/core/db.py": 0})),
        patch("core.db.get_edge_count", new=AsyncMock(return_value=5)),
    ):
        raw = await tool._arun()
    payload = json.loads(raw)
    digest = payload["digest"]
    assert digest["graph_schema"] == {"indexed_files": 2, "edges": 5}
    assert digest["hotspots"]["top"] == [{"file": "core/db.py", "score": 0.9}]
    assert digest["entrypoints"]["top"] == ["main.py"]


async def test_tool_fails_open_to_skeleton_on_exception() -> None:
    tool = ArchitectureDigestTool(project_id="proj", workspace_root="/ws")
    with patch(
        "core.db.list_indexed_files", new=AsyncMock(side_effect=RuntimeError("db boom"))
    ):
        raw = await tool._arun()
    payload = json.loads(raw)
    assert "error" in payload
    assert payload["digest"] == _empty_digest()


# ── Dual-agent reachability ───────────────────────────────────────────────────


async def test_registered_for_analyst_and_researcher(tmp_path: Path) -> None:
    from core.permissions import ToolPrivilegeTier
    from tools.analyst_tools import register_analyst_tools
    from tools.researcher_tools import register_researcher_tools

    store = _isolated_store(tmp_path)
    await register_analyst_tools(store)
    await register_researcher_tools(store)

    schemas = {s.name: s for s in store.all_schemas()}
    assert "architecture_digest" in schemas
    assert schemas["architecture_digest"].privilege_tier == ToolPrivilegeTier.READ_ONLY
    roles = schemas["architecture_digest"].allowed_roles
    assert "analyst" in roles or "researcher" in roles


def test_executable_in_both_build_dicts() -> None:
    from tools.analyst_tools import build_analyst_tools
    from tools.researcher_tools import build_researcher_tools

    state = {"workspace_root": "/ws", "project_id": "proj", "session_id": "s", "task_id": "t"}
    analyst = build_analyst_tools(state)
    researcher = build_researcher_tools(state)

    assert isinstance(analyst["architecture_digest"].tool, ArchitectureDigestTool)
    assert isinstance(researcher["architecture_digest"].tool, ArchitectureDigestTool)
    assert "researcher" in researcher["architecture_digest"].allowed_roles
    assert "analyst" in analyst["architecture_digest"].allowed_roles
