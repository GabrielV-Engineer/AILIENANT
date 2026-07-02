"""Cross-boundary link edges (WS / MCP seams): the Tier-2 boundary graph.

Contracts under test:
  - declares edges are populated for the WS union + MCP catalog from a seeded contract.
  - A real dispatch on the channel literal is `handles`: the frontend `case '...'` switch
    (extension side) and the MCP `{name: handler}` dict key (gateway side).
  - The direction rule is deterministic: `server_*` extension-side → handles; `client_*`
    extension-side → emits, core-side → handles.
  - The quoted-literal confirm rejects substring false-positives (`run_task` ≠ `rerun_task`)
    and backtick/comment prose mentions.
  - A non-conforming channel (no server_/client_ prefix) lands in `references`, never `handles`.
  - The boundary layer never pollutes code-dependency traversal: `get_all_edges` and
    `compute_blast_radius` are identical with and without the boundary graph populated.
  - Concurrent `refresh_boundary_graph` collapses to a single rebuild (single-flight).
  - The tool is reachable, READ_ONLY, via both build_analyst_tools and build_researcher_tools,
    and an empty trace is never phrased as "unhandled".
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from core import blast_radius as _blast_radius_module
from core import boundary_graph
from core import db as catalog_db
from core.blast_radius import compute_blast_radius as _real_compute_blast_radius
from core.boundary_graph import BoundaryRegistry, refresh_boundary_graph, trace_boundary
from core.permissions import ToolPrivilegeTier

_PID = "proj"

# A synthetic contract: two WS channels (one server_, one client_), one non-conforming
# WS channel, and one MCP verb. Declaration-file suffixes match the real repo layout.
_REGISTRY = BoundaryRegistry(
    ws_channels=frozenset({"server_stream_end", "client_hitl_response", "auth"}),
    mcp_channels=frozenset({"run_task"}),
)


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))


async def _seed(tmp_path: Any, rel: str, content: str) -> str:
    """Write a real file (creating parent dirs) and register it in the catalog. Returns path."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    path = str(p)
    await catalog_db.upsert_indexed_file(path, _PID)
    await catalog_db.index_file_lines(path, content, _PID)
    return path


async def _seed_contract_workspace(tmp_path: Any) -> None:
    """Seed a miniature extension/core workspace exercising every direction-rule branch."""
    # Declaration files — quoted channel literals → declares edges.
    await _seed(
        tmp_path, "ailienant-core/api/ws_contracts.py",
        'event_type: Literal["server_stream_end"] = "server_stream_end"\n'
        'event_type: Literal["client_hitl_response"] = "client_hitl_response"\n'
        'event_type: Literal["auth"] = "auth"\n',
    )
    await _seed(
        tmp_path, "ailienant-core/gateway/catalog.py",
        'Capability(name="run_task")\n',
    )
    # Extension handler: a real dispatch on server_stream_end → handles (extension side).
    await _seed(
        tmp_path, "ailienant-extension/src/hook.ts",
        "switch (msg.type) {\n  case 'server_stream_end': break;\n}\n",
    )
    # Extension emit site: a client_ object send → emits (extension side).
    await _seed(
        tmp_path, "ailienant-extension/src/send.ts",
        "post({ event_type: 'client_hitl_response' });\n",
    )
    # Core dispatch on a client_ channel → handles (core side).
    await _seed(
        tmp_path, "ailienant-core/api/ws_router.py",
        'if event_type == "client_hitl_response":\n    handle()\n',
    )
    # MCP handler dict-key literal → handles (gateway side).
    await _seed(
        tmp_path, "ailienant-core/gateway/handlers.py",
        '_HANDLERS = {"run_task": handle_run_task}\n',
    )
    # A non-conforming channel referenced in an extension file → references, never handles.
    await _seed(
        tmp_path, "ailienant-extension/src/auth.ts",
        "send({ event_type: 'auth' });\n",
    )


# ── declares + handles + direction rule ───────────────────────────────────────


def test_declares_and_handles_populated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed_contract_workspace(tmp_path)
        count = await refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY)
        return count, await trace_boundary("server_stream_end", _PID), await trace_boundary("run_task", _PID)

    count, ws_trace, mcp_trace = asyncio.run(_run())
    assert count > 0
    # WS: declared in ws_contracts.py, handled by the extension switch.
    assert any(f.endswith("ws_contracts.py") for f in ws_trace.declared_in)
    assert any(f.endswith("hook.ts") for f in ws_trace.handlers)
    assert ws_trace.seam == "ws"
    # MCP: declared in catalog.py, handled by the gateway dict.
    assert any(f.endswith("catalog.py") for f in mcp_trace.declared_in)
    assert any(f.endswith("handlers.py") for f in mcp_trace.handlers)
    assert mcp_trace.seam == "mcp"


def test_client_direction_rule(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed_contract_workspace(tmp_path)
        await refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY)
        return await trace_boundary("client_hitl_response", _PID)

    trace = asyncio.run(_run())
    # Extension send site emits; core dispatch handles (deterministic side × prefix rule).
    assert any(f.endswith("send.ts") for f in trace.emitters)
    assert any(f.endswith("ws_router.py") for f in trace.handlers)


def test_non_conforming_channel_is_references(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        await _seed_contract_workspace(tmp_path)
        await refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY)
        return await trace_boundary("auth", _PID)

    trace = asyncio.run(_run())
    assert any(f.endswith("auth.ts") for f in trace.references)
    assert trace.handlers == []  # no prefix → role undetermined → never a handler


# ── quoted-literal confirm: substring + prose rejection ───────────────────────


def test_quoted_literal_confirm_rejects_substring_and_prose(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Any:
        await catalog_db.init_db()
        # A substring collision, a backtick mention, and a bare-comment mention — no edges.
        await _seed(
            tmp_path, "ailienant-extension/src/noise.ts",
            "call('rerun_task');\n"          # substring of run_task, quote precedes 're'
            "// see `server_stream_end`\n"    # backtick prose, not a quoted literal
            "/* server_stream_end docs */\n",  # bare comment mention
        )
        await refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY)
        return (
            await trace_boundary("run_task", _PID),
            await trace_boundary("server_stream_end", _PID),
        )

    mcp_trace, ws_trace = asyncio.run(_run())
    noise = "noise.ts"
    for trace in (mcp_trace, ws_trace):
        for bucket in (trace.handlers, trace.emitters, trace.references, trace.declared_in):
            assert not any(f.endswith(noise) for f in bucket)


# ── non-pollution: code-dependency traversal is untouched ─────────────────────


def test_code_dependency_traversal_unaffected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    # Restore the real blast-radius resolver (conftest stubs it to [] for hermetic units).
    monkeypatch.setattr(
        _blast_radius_module, "compute_blast_radius", _real_compute_blast_radius
    )

    async def _run() -> Any:
        await catalog_db.init_db()
        # A plain code-dependency edge, plus the whole boundary contract.
        await _seed(tmp_path, "ailienant-core/a.py", "import b\n")
        await catalog_db.upsert_dependencies(
            str(tmp_path / "ailienant-core/a.py"), ["b"], _PID
        )
        b_path = await _seed(tmp_path, "ailienant-core/b.py", "x = 1\n")
        await _seed_contract_workspace(tmp_path)

        edges_before = sorted(await catalog_db.get_all_edges(_PID))
        radius_before = sorted(
            await _real_compute_blast_radius(_PID, [b_path], depth=1, workspace_root=str(tmp_path))
        )
        n = await refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY)
        edges_after = sorted(await catalog_db.get_all_edges(_PID))
        radius_after = sorted(
            await _real_compute_blast_radius(_PID, [b_path], depth=1, workspace_root=str(tmp_path))
        )
        return n, edges_before, edges_after, radius_before, radius_after

    n, edges_before, edges_after, radius_before, radius_after = asyncio.run(_run())
    assert n > 0                                # boundary edges WERE populated
    assert edges_before == edges_after          # dependency_graph untouched
    assert radius_before == radius_after        # blast-radius traversal untouched
    # No boundary channel leaked into the code-dependency edge set.
    assert not any(t in {"server_stream_end", "run_task", "auth"} for _s, t in edges_after)


# ── single-flight rebuild ─────────────────────────────────────────────────────


def test_refresh_is_single_flight(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def _fake_do_refresh(*_a: Any, **_k: Any) -> int:
        calls["n"] += 1
        await asyncio.sleep(0.02)  # hold the in-flight window open for the second caller
        return 7

    monkeypatch.setattr(boundary_graph, "_do_refresh", _fake_do_refresh)

    async def _run() -> Any:
        await catalog_db.init_db()
        return await asyncio.gather(
            refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY),
            refresh_boundary_graph(_PID, str(tmp_path), registry=_REGISTRY),
        )

    results = asyncio.run(_run())
    assert results == [7, 7]   # both callers get the same rebuild result
    assert calls["n"] == 1     # ...from a single rebuild


# ── tool reachability + advisory framing ──────────────────────────────────────


def test_tool_reachable_read_only_via_both_builds() -> None:
    from tools.analyst_tools import build_analyst_tools
    from tools.perception_tools import TraceCrossBoundaryTool
    from tools.researcher_tools import build_researcher_tools

    state = {"workspace_root": "/w", "project_id": "p", "session_id": "s", "task_id": "t"}
    for build in (build_analyst_tools, build_researcher_tools):
        reg = build(state).get("trace_cross_boundary")
        assert reg is not None, f"{build.__name__} did not wire trace_cross_boundary"
        assert isinstance(reg.tool, TraceCrossBoundaryTool)
        assert reg.tier == ToolPrivilegeTier.READ_ONLY


def test_tool_empty_trace_never_says_unhandled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    from tools.perception_tools import TraceCrossBoundaryTool

    async def _run() -> str:
        await catalog_db.init_db()
        tool = TraceCrossBoundaryTool(project_id=_PID, workspace_root=str(tmp_path))
        return await tool._arun(channel="server_never_declared")

    payload = json.loads(asyncio.run(_run()))
    assert payload["handlers"] == []
    assert payload["in_catalog"] is False
    # The advisory note must not assert the channel is unhandled/dead.
    note = payload.get("note", "")
    assert "NOT" in note and "unhandled" in note.lower()
