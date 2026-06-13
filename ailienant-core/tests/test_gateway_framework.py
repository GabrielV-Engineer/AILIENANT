# ailienant-core/tests/test_gateway_framework.py
#
# Framework tests for the External Capability Gateway: catalog/schema validity,
# the call-tool routing seam, and host-discovery read/write/probe. No real
# subprocess is spawned — the server handlers are exercised in-process.

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from typing import Any, Dict

import mcp.types as types
import pytest
from jsonschema import Draft7Validator  # type: ignore[import-untyped]

from core.config import host_discovery
from core.config.host_discovery import (
    HostCoords,
    HostNotRunningError,
    probe_host_alive,
    read_run_state,
    resolve_host_or_error,
    write_run_state,
)
from core.permissions import ToolPrivilegeTier
from gateway import catalog, server


# ---------------------------------------------------------------------------
# Catalog & schema (the DoD: list the catalog, retrieve valid schemas)
# ---------------------------------------------------------------------------


def test_catalog_exposes_all_seven_capabilities() -> None:
    names = {cap.name for cap in catalog.CATALOG}
    assert names == {
        "run_task",
        "run_benchmark",
        "check_task_status",
        "query_memory",
        "get_dependents",
        "get_workspace_graph",
        "get_report",
    }


def test_each_capability_has_a_valid_json_schema() -> None:
    for cap in catalog.CATALOG:
        # Raises SchemaError if the declared input schema is not valid JSON-Schema.
        Draft7Validator.check_schema(cap.input_schema)


def test_to_mcp_tools_projects_catalog_faithfully() -> None:
    tools = catalog.to_mcp_tools()
    assert len(tools) == len(catalog.CATALOG)
    by_name = {t.name: t for t in tools}
    for cap in catalog.CATALOG:
        tool = by_name[cap.name]
        assert tool.inputSchema == cap.input_schema
        Draft7Validator.check_schema(tool.inputSchema)
        # Tier and schema version are surfaced on the wire via _meta + annotations.
        assert tool.meta is not None
        assert tool.meta["schema_version"] == cap.schema_version
        assert tool.meta["tier"] == cap.tier.value
        is_read_only = cap.tier == ToolPrivilegeTier.READ_ONLY
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is is_read_only


def test_list_tools_advertises_protocol_version() -> None:
    """Every tool surfaces the surface semver (D7) alongside its schema version."""
    for tool in catalog.to_mcp_tools():
        assert tool.meta is not None
        assert tool.meta["protocol_version"] == catalog.PROTOCOL_VERSION
        # A non-deprecated v1 verb carries the flag but no null detail keys.
        assert tool.meta["deprecated"] is False
        assert "deprecated_since" not in tool.meta
        assert "sunset_version" not in tool.meta


def test_deprecated_capability_surfaces_sunset_metadata() -> None:
    """A deprecated capability advertises its since/sunset so callers can migrate."""
    live = catalog._tool_meta(
        catalog.Capability(
            name="live_verb",
            description="d",
            tier=ToolPrivilegeTier.READ_ONLY,
            input_schema={"type": "object"},
            is_async=False,
        )
    )
    assert live["deprecated"] is False

    sunset = catalog._tool_meta(
        catalog.Capability(
            name="old_verb",
            description="d",
            tier=ToolPrivilegeTier.READ_ONLY,
            input_schema={"type": "object"},
            is_async=False,
            deprecated=True,
            deprecated_since="1.1.0",
            sunset_version="2.0.0",
        )
    )
    assert sunset["deprecated"] is True
    assert sunset["deprecated_since"] == "1.1.0"
    assert sunset["sunset_version"] == "2.0.0"


def test_async_verbs_are_execute_tier_and_have_a_poll_companion() -> None:
    async_names = {cap.name for cap in catalog.CATALOG if cap.is_async}
    assert async_names == {"run_task", "run_benchmark"}
    for cap in catalog.CATALOG:
        if cap.is_async:
            assert cap.tier == ToolPrivilegeTier.EXECUTE
    # The poll companion that makes the async contract usable over JSON-RPC.
    assert catalog.get_capability("check_task_status") is not None


# ---------------------------------------------------------------------------
# Server wiring & call-tool routing seam
# ---------------------------------------------------------------------------


def test_build_gateway_server_registers_handlers() -> None:
    # Building the real server binds the capability handlers into the module-global
    # registry; snapshot/restore so it cannot leak into the unwired-verb assertions
    # below (or other modules).
    saved = dict(server._HANDLERS)
    try:
        srv = server.build_gateway_server()
        assert types.ListToolsRequest in srv.request_handlers
        assert types.CallToolRequest in srv.request_handlers
    finally:
        server._HANDLERS.clear()
        server._HANDLERS.update(saved)


def test_dispatch_unknown_capability_returns_error_envelope() -> None:
    result = asyncio.run(server.dispatch_call("does_not_exist", {}))
    assert isinstance(result, list)
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert payload == {
        "status": "error",
        "reason": "unknown_capability",
        "capability": "does_not_exist",
    }


def test_dispatch_known_but_unwired_capability_returns_not_implemented() -> None:
    result = asyncio.run(server.dispatch_call("query_memory", {"query": "x"}))
    payload = json.loads(result[0].text)
    assert payload == {"status": "not_implemented", "capability": "query_memory"}


def test_dispatch_routes_to_a_registered_handler() -> None:
    async def _fake(arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {"echo": arguments}

    server.register_handler("query_memory", _fake)
    try:
        result = asyncio.run(server.dispatch_call("query_memory", {"query": "x"}))
        payload = json.loads(result[0].text)
        assert payload == {
            "status": "ok",
            "capability": "query_memory",
            "result": {"echo": {"query": "x"}},
        }
    finally:
        server._HANDLERS.pop("query_memory", None)


# ---------------------------------------------------------------------------
# Host discovery (D1a): write / read / probe / resolve
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_run_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / ".ailienant" / "run.json"
    monkeypatch.setattr(host_discovery, "RUN_STATE_PATH", path)
    return path


def test_write_run_state_is_owner_only_and_round_trips(isolated_run_state: Any) -> None:
    write_run_state(54321, "secret-token", os.getpid())
    assert isolated_run_state.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(isolated_run_state).st_mode)
        assert mode == 0o600
    coords = read_run_state()
    assert coords == HostCoords(port=54321, token="secret-token", pid=os.getpid())


def test_read_run_state_missing_returns_none(isolated_run_state: Any) -> None:
    assert read_run_state() is None


def test_read_run_state_malformed_returns_none(isolated_run_state: Any) -> None:
    isolated_run_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_run_state.write_text("{not valid json", encoding="utf-8")
    assert read_run_state() is None


def test_probe_alive_true_against_live_listener() -> None:
    async def _scenario() -> bool:
        srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            coords = HostCoords(port=port, token=None, pid=os.getpid())
            return await probe_host_alive(coords, timeout_sec=2.0)
        finally:
            srv.close()
            await srv.wait_closed()

    assert asyncio.run(_scenario()) is True


def test_probe_alive_false_against_dead_port() -> None:
    # Bind to grab a free port, then close it so nothing is listening.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    coords = HostCoords(port=dead_port, token=None, pid=os.getpid())
    assert asyncio.run(probe_host_alive(coords, timeout_sec=1.0)) is False


def test_resolve_host_or_error_raises_when_stale(isolated_run_state: Any) -> None:
    # A run file that points at a dead port is stale and must be rejected.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    write_run_state(dead_port, "tok", os.getpid())
    with pytest.raises(HostNotRunningError):
        asyncio.run(resolve_host_or_error())


def test_resolve_host_or_error_raises_when_absent(isolated_run_state: Any) -> None:
    with pytest.raises(HostNotRunningError):
        asyncio.run(resolve_host_or_error())
