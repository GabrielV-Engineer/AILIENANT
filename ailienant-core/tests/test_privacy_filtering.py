"""Dual-rules privacy filtering — exclude_patterns in .ailienant.json.

Covers:
  - RuleManager.is_excluded(): gitwildmatch glob matching, no-match, missing key.
  - VFSMiddleware.read_safe(): Layer 0 blocks before Layer 1 on excluded files.
"""
import json
import os
from pathlib import Path
from typing import Generator

import pytest

from core.rules import RuleManager, rule_manager
from core.vfs_middleware import VFSMiddleware


@pytest.fixture(autouse=True)
def _reset_rule_manager() -> Generator[None, None, None]:
    """Isolate singleton cache between tests."""
    yield
    rule_manager.reset()
    RuleManager._instance = None  # type: ignore[assignment]


def _write_local_config(root: Path, config: dict) -> None:  # type: ignore[type-arg]
    cfg_dir = root / ".ailienant"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".ailienant.json").write_text(json.dumps(config), encoding="utf-8")


# ---------------------------------------------------------------------------
# RuleManager.is_excluded
# ---------------------------------------------------------------------------


def test_is_excluded_matches_glob(tmp_path: Path) -> None:
    """A file under node_modules matches the gitwildmatch pattern."""
    _write_local_config(tmp_path, {"exclude_patterns": ["**/node_modules/**"]})
    rm = RuleManager()
    filepath = str(tmp_path / "src" / "node_modules" / "pkg" / "index.js")
    assert rm.is_excluded(filepath, str(tmp_path)) is True


def test_is_excluded_no_match(tmp_path: Path) -> None:
    """A normal source file does not match the node_modules pattern."""
    _write_local_config(tmp_path, {"exclude_patterns": ["**/node_modules/**"]})
    rm = RuleManager()
    filepath = str(tmp_path / "src" / "main.py")
    assert rm.is_excluded(filepath, str(tmp_path)) is False


def test_is_excluded_missing_key(tmp_path: Path) -> None:
    """A config without exclude_patterns never raises and always returns False."""
    _write_local_config(tmp_path, {"rules": ["Be concise."]})
    rm = RuleManager()
    filepath = str(tmp_path / "secret.env")
    assert rm.is_excluded(filepath, str(tmp_path)) is False


def test_global_patterns_not_dropped_by_local(tmp_path: Path) -> None:
    """Global exclude_patterns union-merge with local — neither side is lost."""
    # Simulate global config via a temp home dir trick: write a second local
    # config with only the global pattern and verify both survive after compose.
    local_cfg = tmp_path / ".ailienant" / ".ailienant.json"
    local_cfg.parent.mkdir(parents=True, exist_ok=True)
    local_cfg.write_text(
        json.dumps({"exclude_patterns": ["**/node_modules/**"]}), encoding="utf-8"
    )

    # Write a fake global config in a second temp dir then compose manually
    # via _compose to confirm both lists are concat+deduped.
    global_data = {"exclude_patterns": ["**/.env", "**/.secret"]}
    local_data = {"exclude_patterns": ["**/node_modules/**"]}
    rm = RuleManager()
    merged = rm._compose(local_data, global_data)
    patterns = merged.get("exclude_patterns", [])
    assert "**/node_modules/**" in patterns
    assert "**/.env" in patterns
    assert "**/.secret" in patterns


# ---------------------------------------------------------------------------
# VFSMiddleware.read_safe — Layer 0 gate
# ---------------------------------------------------------------------------


def test_vfs_read_safe_returns_file_excluded(tmp_path: Path) -> None:
    """read_safe() returns FILE_EXCLUDED for a file matching exclude_patterns."""
    # Create a real file so Layer 1/2/3 would otherwise pass it through.
    secret = tmp_path / "credentials.json"
    secret.write_text('{"api_key": "s3cr3t"}', encoding="utf-8")

    _write_local_config(tmp_path, {"exclude_patterns": ["**/credentials.json"]})

    vfs = VFSMiddleware()
    result = vfs.read_safe(str(secret), project_id="proj-1", project_root=str(tmp_path))

    assert result.ok is False
    assert result.error == "FILE_EXCLUDED"
    assert result.content is None
