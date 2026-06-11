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
        # Execution metadata defaults: enabled (bool-coerced), global scope.
        assert rows[0]["enabled"] is True
        assert rows[0]["scope"] == "global"
        assert rows[0]["description"] is None
        await catalog_db.upsert_skill("s1", "Audit v2", "Updated body")
        rows = await catalog_db.list_skills()
        assert len(rows) == 1 and rows[0]["name"] == "Audit v2"  # upsert, not duplicate
        # get_skill returns the single row with the new fields.
        one = await catalog_db.get_skill("s1")
        assert one is not None and one["body"] == "Updated body"
        assert await catalog_db.get_skill("missing") is None
        await catalog_db.delete_skill("s1")
        assert await catalog_db.list_skills() == []

    asyncio.run(_run())


def test_skills_scope_query(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """list_enabled_skills_for_scope returns global + the matching workspace, and
    skips disabled rows. An empty workspace_root selects global only."""
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        await catalog_db.upsert_skill("g1", "Global", "g", scope="global")
        await catalog_db.upsert_skill(
            "w1", "Local", "w", scope="workspace", workspace_root="/ws/a"
        )
        await catalog_db.upsert_skill(
            "w2", "Other", "o", scope="workspace", workspace_root="/ws/b"
        )
        await catalog_db.upsert_skill("d1", "Disabled", "d", enabled=False)

        names_a = {r["name"] for r in await catalog_db.list_enabled_skills_for_scope("/ws/a")}
        assert names_a == {"Global", "Local"}  # global + this workspace, not /ws/b
        assert "Disabled" not in names_a  # enabled=0 filtered out

        names_empty = {r["name"] for r in await catalog_db.list_enabled_skills_for_scope("")}
        assert names_empty == {"Global"}  # global only — workspace rows never hidden via ''

    asyncio.run(_run())


def test_save_skill_scope_validation(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from api import skills as skills_api

    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        # workspace scope without a workspace_root is rejected.
        bad = await skills_api.save_skill(
            {"name": "X", "body": "b", "scope": "workspace"}
        )
        assert bad["ok"] is False and "workspace_root" in bad["error"]
        # an unknown scope is rejected.
        bad2 = await skills_api.save_skill({"name": "X", "body": "b", "scope": "weird"})
        assert bad2["ok"] is False
        # a well-formed skill persists the new fields.
        ok = await skills_api.save_skill(
            {"name": "Sec", "body": "b", "description": "security review", "scope": "global"}
        )
        assert ok["ok"] is True
        rows = await catalog_db.list_skills()
        assert rows[0]["description"] == "security review"

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
