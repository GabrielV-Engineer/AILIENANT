"""Unit tests for the backend-masked MCP credential store.

The store persists per-server environment secrets, masks them on read, and
never lets a re-submitted masked placeholder clobber the real stored value.
"""
from pathlib import Path
from typing import Any

import pytest

from core.config import mcp_secrets


def _isolate(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mcp_secrets.json"
    monkeypatch.setattr(mcp_secrets, "MCP_SECRETS_PATH", path)
    return path


def test_set_get_round_trip(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mcp_secrets.set_server_secrets("github", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret123"})
    assert mcp_secrets.get_server_env("github") == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret123"}


def test_get_masked_never_returns_raw(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mcp_secrets.set_server_secrets("github", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_supersecret"})
    masked = mcp_secrets.mask_server_secrets("github")["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert "supersecret" not in masked
    assert mcp_secrets.is_masked(masked)


def test_file_is_owner_only_permissions(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import stat

    path = _isolate(tmp_path, monkeypatch)
    mcp_secrets.set_server_secrets("brave-search", {"BRAVE_API_KEY": "abc"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    # POSIX: owner-only (0600). Windows toggles only the read-only bit, so this
    # assertion is meaningful on POSIX and trivially satisfied on Windows.
    if os.name == "posix":
        assert mode == 0o600


def test_masked_resubmit_does_not_overwrite(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mcp_secrets.set_server_secrets("postgres", {"POSTGRES_CONNECTION_STRING": "postgres://real"})
    masked = mcp_secrets.mask_server_secrets("postgres")["POSTGRES_CONNECTION_STRING"]
    # A masked value coming back from the UI must be ignored, not stored.
    mcp_secrets.set_server_secrets("postgres", {"POSTGRES_CONNECTION_STRING": masked})
    assert mcp_secrets.get_server_env("postgres") == {"POSTGRES_CONNECTION_STRING": "postgres://real"}


def test_delete_removes_secrets(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mcp_secrets.set_server_secrets("github", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp"})
    mcp_secrets.delete_server_secrets("github")
    assert mcp_secrets.get_server_env("github") == {}


def test_unknown_server_is_empty(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch)
    assert mcp_secrets.get_server_env("nope") == {}
    assert mcp_secrets.mask_server_secrets("nope") == {}


def test_corrupt_file_degrades_to_empty(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _isolate(tmp_path, monkeypatch)
    path.write_text("{not json", encoding="utf-8")
    assert mcp_secrets.load_mcp_secrets() == {}
