"""Phase 7.9.A.7 — Command-menu config stores + endpoints.

Covers the WAL-mode catalog CRUD for skills / mcp_servers / hooks /
agent_role_overrides, the scalar settings round-trip (output_style +
permission_mode with validation), and the zombie-safe MCP /test probe
(an unreachable URI returns reachable=False fast, never hangs).

Async cases run via ``asyncio.run`` (no pytest-asyncio). The catalog DB and
the settings file are isolated per-test via the ``DB_CATALOG_PATH`` /
``_SETTINGS_PATH`` seams onto ``tmp_path``.
"""
import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from api import mcp_servers as mcp_api
from api import system_settings
from core import db as catalog_db


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def test_config_tables_created(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _isolate_catalog(tmp_path, monkeypatch)
    asyncio.run(catalog_db.init_db())
    conn = sqlite3.connect(db)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('skills','mcp_servers','hooks','agent_role_overrides')"
        )
    }
    conn.close()
    assert names == {"skills", "mcp_servers", "hooks", "agent_role_overrides"}


def test_skills_crud(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_skill("s1", "Audit", "Do a security audit")
        rows = await catalog_db.list_skills()
        assert len(rows) == 1 and rows[0]["name"] == "Audit"
        await catalog_db.upsert_skill("s1", "Audit v2", "Updated body")
        rows = await catalog_db.list_skills()
        assert len(rows) == 1 and rows[0]["name"] == "Audit v2"  # upsert, not duplicate
        await catalog_db.delete_skill("s1")
        assert await catalog_db.list_skills() == []

    asyncio.run(_run())


def test_mcp_servers_crud(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_mcp_server("m1", "Local", "stdio:///srv", enabled=False)
        rows = await catalog_db.list_mcp_servers()
        assert len(rows) == 1
        assert rows[0]["enabled"] is False  # coerced to bool
        assert rows[0]["transport"] == "stdio"
        await catalog_db.delete_mcp_server("m1")
        assert await catalog_db.list_mcp_servers() == []

    asyncio.run(_run())


def test_hooks_crud(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_hook("h1", "post_patch", "npm run lint", enabled=True)
        rows = await catalog_db.list_hooks()
        assert len(rows) == 1 and rows[0]["enabled"] is True
        await catalog_db.upsert_hook("h1", "post_patch", "npm run lint", enabled=False)
        rows = await catalog_db.list_hooks()
        assert rows[0]["enabled"] is False
        await catalog_db.delete_hook("h1")
        assert await catalog_db.list_hooks() == []

    asyncio.run(_run())


def test_agent_overrides_crud(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_agent_override("core_dev", "Be extra careful")
        assert (await catalog_db.list_agent_overrides())["core_dev"] == "Be extra careful"
        await catalog_db.delete_agent_override("core_dev")
        assert await catalog_db.list_agent_overrides() == {}

    asyncio.run(_run())


def test_settings_scalars_roundtrip(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_settings, "_SETTINGS_PATH", Path(tmp_path) / "settings.json")

    async def _run() -> None:
        saved = await system_settings.save_settings(
            {"output_style": "concise", "permission_mode": "plan"}
        )
        assert saved["output_style"] == "concise"
        assert saved["permission_mode"] == "plan"
        # Invalid values fall back to default (never persisted raw).
        saved = await system_settings.save_settings(
            {"output_style": "bogus", "permission_mode": "nope"}
        )
        assert saved["output_style"] == "default"
        assert saved["permission_mode"] == "default"
        fetched = await system_settings.get_settings()
        assert fetched["analyst_name"] == "Natt"  # default preserved

    asyncio.run(_run())


def test_mcp_test_unreachable_is_fast_and_safe() -> None:
    """A bogus stdio URI returns reachable=False without raising or hanging."""
    result = asyncio.run(mcp_api.test_server({"uri": "stdio:///nonexistent/ailienant/probe"}))
    assert result["reachable"] is False
    assert result["tool_count"] == 0

    missing = asyncio.run(mcp_api.test_server({}))
    assert missing["reachable"] is False
    assert "uri" in (missing.get("error") or "")
