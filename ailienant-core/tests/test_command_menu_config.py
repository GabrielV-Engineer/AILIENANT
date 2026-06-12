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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api import mcp_servers as mcp_api
from api import system_settings
from core import db as catalog_db
from core.config import mcp_secrets
from tools import mcp_adapter


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


def _isolate_secrets(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "MCP_SECRETS_PATH", Path(tmp_path) / "mcp_secrets.json")


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


# ---------------------------------------------------------------------------
# Browse-and-install from the curated registry
# ---------------------------------------------------------------------------


def test_registry_lists_curated_servers(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        out = await mcp_api.list_registry()
        names = {s["name"] for s in out["servers"]}
        assert {"github", "docker", "postgres", "brave-search"} <= names
        assert all(s["installed"] is False for s in out["servers"])  # nothing installed yet

    asyncio.run(_run())


def test_install_secretless_server(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)
    mcp_adapter._reset_mcp_session_for_tests()

    async def _run() -> None:
        await catalog_db.init_db()
        with patch.object(mcp_api, "bootstrap_mcp_session", AsyncMock(return_value=False)):
            res = await mcp_api.install_from_registry({"name": "docker"})
        assert res["ok"] is True
        rows = await catalog_db.list_mcp_servers()
        assert len(rows) == 1 and rows[0]["name"] == "docker"
        # The persisted uri parses back to the registry's command + args.
        params = mcp_adapter._parse_mcp_uri(rows[0]["uri"])
        assert params.command == "uvx"

    asyncio.run(_run())


def test_install_credentialed_server_stores_masked(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)
    mcp_adapter._reset_mcp_session_for_tests()

    async def _run() -> None:
        await catalog_db.init_db()
        with patch.object(mcp_api, "bootstrap_mcp_session", AsyncMock(return_value=False)):
            res = await mcp_api.install_from_registry(
                {"name": "github", "secrets": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secretvalue"}}
            )
        assert res["ok"] is True
        # Stored for injection, masked for display — the raw value never echoes.
        assert mcp_secrets.get_server_env("github")["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_secretvalue"
        masked = mcp_secrets.mask_server_secrets("github")["GITHUB_PERSONAL_ACCESS_TOKEN"]
        assert "secretvalue" not in masked

    asyncio.run(_run())


def test_install_rejects_unknown_name(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        res = await mcp_api.install_from_registry({"name": "totally-not-real"})
        assert res["ok"] is False
        assert await catalog_db.list_mcp_servers() == []

    asyncio.run(_run())


def test_install_rejects_unknown_secret_key(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        res = await mcp_api.install_from_registry(
            {"name": "github", "secrets": {"NOT_A_DECLARED_SECRET": "x"}}
        )
        assert res["ok"] is False
        assert "Unknown secret" in res["error"]

    asyncio.run(_run())


def test_install_rejects_missing_required_secret(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)

    async def _run() -> None:
        await catalog_db.init_db()
        res = await mcp_api.install_from_registry({"name": "github"})
        assert res["ok"] is False
        assert "Missing required secret" in res["error"]

    asyncio.run(_run())


def test_reinstall_closes_prior_session(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)
    mcp_adapter._reset_mcp_session_for_tests()
    mcp_adapter._sessions["github"] = MagicMock()  # pretend a live session exists

    async def _run() -> None:
        await catalog_db.init_db()
        close_spy = AsyncMock()
        with (
            patch.object(mcp_api, "bootstrap_mcp_session", AsyncMock(return_value=False)),
            patch.object(mcp_api, "close_mcp_session", close_spy),
        ):
            res = await mcp_api.install_from_registry(
                {"name": "github", "secrets": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_new"}}
            )
        assert res["ok"] is True
        close_spy.assert_awaited_once_with("github")

    try:
        asyncio.run(_run())
    finally:
        mcp_adapter._reset_mcp_session_for_tests()


def test_delete_wipes_secret_and_session(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_catalog(tmp_path, monkeypatch)
    _isolate_secrets(tmp_path, monkeypatch)
    mcp_adapter._reset_mcp_session_for_tests()

    async def _run() -> None:
        await catalog_db.init_db()
        with patch.object(mcp_api, "bootstrap_mcp_session", AsyncMock(return_value=False)):
            await mcp_api.install_from_registry(
                {"name": "github", "secrets": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}}
            )
        assert mcp_secrets.get_server_env("github") != {}
        with patch.object(mcp_api, "close_mcp_session", AsyncMock()) as close_spy:
            await mcp_api.remove_server("github")  # id == name for registry installs
            close_spy.assert_awaited_once_with("github")
        assert mcp_secrets.get_server_env("github") == {}

    asyncio.run(_run())
