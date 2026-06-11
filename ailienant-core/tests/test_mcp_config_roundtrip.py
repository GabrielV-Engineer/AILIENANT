# ailienant-core/tests/test_mcp_config_roundtrip.py
#
# Round-trip tests for the .ailienant/config.json projection of the MCP server
# catalog: export must not leak credentials, import must reconcile by name
# without duplicating servers, and a malformed/unsupported payload must fail
# fast. The catalog DB is isolated per-test via the DB_CATALOG_PATH seam.

import asyncio
import json
from typing import Any

import pytest

from api import mcp_servers as mcp_api
from core import db as catalog_db
from core import mcp_config
from core.mcp_config import (
    McpConfigError,
    export_mcp_config,
    import_mcp_config,
)

_VALIDATE = mcp_api._validate_mcp_command

# An allowlisted launcher (npx) — passes the command guard on import.
_NPX = "stdio:///usr/bin/npx?arg=-y&arg=@modelcontextprotocol/server-github"
_NPX_PG = (
    "stdio:///usr/bin/npx?arg=-y&arg=@modelcontextprotocol/server-postgres"
    "&arg=postgresql://admin:hunter2@db.internal/prod"
)


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def test_export_shape_and_key_ref(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_mcp_server("g1", "github", _NPX, "stdio", True)
        await catalog_db.upsert_mcp_server("c1", "my-tool", _NPX, "stdio", True)
        cfg = await export_mcp_config()
        assert cfg["version"] == mcp_config.MCP_CONFIG_VERSION
        by_name = {s["name"]: s for s in cfg["servers"]}
        assert by_name["github"]["key_ref"] == "vscode_secret:github"
        assert "key_ref" not in by_name["my-tool"]  # not a regulated server

    asyncio.run(_run())


def test_export_redacts_uri_credentials(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_mcp_server("p1", "postgres", _NPX_PG, "stdio", True)
        cfg = await export_mcp_config()
        blob = json.dumps(cfg)
        # The credential embedded in the uri must never reach the projection.
        assert "hunter2" not in blob
        assert "admin:hunter2" not in blob
        assert "<redacted>@" in blob
        # Only key_ref placeholders carry secret intent — no value-bearing field.
        for server in cfg["servers"]:
            assert "env" not in server and "api_key" not in server
            if "key_ref" in server:
                assert server["key_ref"].startswith("vscode_secret:")

    asyncio.run(_run())


def test_import_is_idempotent_by_name(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_mcp_server("g1", "github", _NPX, "stdio", True)
        cfg = await export_mcp_config()
        await import_mcp_config(cfg, validate_uri=_VALIDATE)
        await import_mcp_config(cfg, validate_uri=_VALIDATE)  # second import: no dup
        rows = await catalog_db.list_mcp_servers()
        assert len(rows) == 1
        assert [r["name"] for r in rows] == ["github"]

    asyncio.run(_run())


def test_reconcile_reuses_id_case_insensitively(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_mcp_server("A", "github", _NPX, "stdio", True)
        # Import the same server under a different capitalization, no id.
        payload = {
            "version": 1,
            "servers": [{"name": "GitHub", "uri": _NPX, "transport": "stdio", "enabled": True}],
        }
        result = await import_mcp_config(payload, validate_uri=_VALIDATE)
        rows = await catalog_db.list_mcp_servers()
        assert len(rows) == 1  # no duplicate from the case difference
        assert rows[0]["id"] == "A"  # updated in place, original id preserved
        assert result["updated"] == ["GitHub"] and result["imported"] == []

    asyncio.run(_run())


def test_import_skips_disallowed_command_without_aborting(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        payload = {
            "version": 1,
            "servers": [
                {"name": "evil", "uri": "stdio:///bin/bash", "transport": "stdio"},
                {"name": "good", "uri": _NPX, "transport": "stdio"},
            ],
        }
        result = await import_mcp_config(payload, validate_uri=_VALIDATE)
        skipped_names = {s["name"] for s in result["skipped"]}
        assert "evil" in skipped_names  # bash command not allowlisted
        assert result["imported"] == ["good"]  # legit server still imported
        rows = {r["name"] for r in await catalog_db.list_mcp_servers()}
        assert rows == {"good"}

    asyncio.run(_run())


def test_malformed_and_unsupported_version_reject(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        with pytest.raises(McpConfigError):
            await import_mcp_config({}, validate_uri=_VALIDATE)
        with pytest.raises(McpConfigError):
            await import_mcp_config({"version": 1}, validate_uri=_VALIDATE)
        with pytest.raises(McpConfigError, match="unsupported config version"):
            await import_mcp_config({"version": 999, "servers": []}, validate_uri=_VALIDATE)

    asyncio.run(_run())


def test_import_endpoint_returns_422_on_malformed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        with pytest.raises(HTTPException) as exc_info:
            await mcp_api.import_config({"version": 999, "servers": []})
        assert exc_info.value.status_code == 422

    asyncio.run(_run())


def test_fresh_machine_roundtrip_flags_needs_secret(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        # A config landing on a fresh machine: a regulated server + a custom one.
        payload = {
            "version": 1,
            "servers": [
                {"name": "github", "uri": _NPX, "transport": "stdio", "enabled": True,
                 "key_ref": "vscode_secret:github"},
                {"name": "my-tool", "uri": _NPX, "transport": "stdio", "enabled": True},
            ],
        }
        result = await import_mcp_config(payload, validate_uri=_VALIDATE)
        assert result["needs_secret"] == ["github"]  # credential pending, not an error
        # Re-export and re-import: projection is stable (lossless over canonical fields).
        export1 = await export_mcp_config()
        await import_mcp_config(export1, validate_uri=_VALIDATE)
        export2 = await export_mcp_config()
        assert export1 == export2

    asyncio.run(_run())
