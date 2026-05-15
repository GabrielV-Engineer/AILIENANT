# core/config/profile.py
"""Phase 3.4.1 — Intelligence Profile config persistence.

Generates and reads `<workspace_root>/.ailienant/.ailienant.json`.
Schema is strict Pydantic; downstream consumers (later phases) use these
thresholds to bias the cascade routing decision.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("PROFILE_CONFIG")

IntelligenceProfile = Literal["Medium", "Big", "Cloud", "Hybrid"]

CONFIG_DIRNAME: str = ".ailienant"
CONFIG_FILENAME: str = ".ailienant.json"


class CascadeThresholds(BaseModel):
    """Default thresholds consumed by future cascade logic (not yet wired)."""
    max_files_medium: int = Field(default=3, ge=1)
    max_files_big: int = Field(default=10, ge=1)
    max_files_cloud: int = Field(default=5, ge=1)
    blast_radius_hybrid: int = Field(default=8, ge=1)
    cascade_l2_threshold: int = Field(default=3, ge=1)


class IntelligenceProfileConfig(BaseModel):
    """On-disk schema for .ailienant/.ailienant.json."""
    master_enabled: bool = Field(default=False)
    profile: IntelligenceProfile = Field(default="Hybrid")
    thresholds: CascadeThresholds = Field(default_factory=CascadeThresholds)


class WorkspaceRootMissingError(ValueError):
    """Raised when no folder workspace is open (e.g. single-file VS Code session)."""


def _config_path(workspace_root: Optional[str]) -> Path:
    """Resolve `<workspace_root>/.ailienant/.ailienant.json`.

    VS Code can open a single file without any folder workspace, in which case
    the extension passes workspace_root as None / "". The profile is a
    workspace-scoped artifact — refusing to write it loudly is safer than
    silently writing to CWD or a tmp dir (which would never be reloaded).
    """
    if not workspace_root or not workspace_root.strip():
        raise WorkspaceRootMissingError(
            "Cannot resolve .ailienant config path: no folder workspace is open. "
            "Open a folder in VS Code (File → Open Folder) before changing the profile."
        )
    return Path(workspace_root) / CONFIG_DIRNAME / CONFIG_FILENAME


def load_from_workspace(workspace_root: Optional[str]) -> IntelligenceProfileConfig:
    """Read & validate the config file. Returns defaults if missing or workspace-less."""
    try:
        p = _config_path(workspace_root)
    except WorkspaceRootMissingError:
        return IntelligenceProfileConfig()
    if not p.exists():
        return IntelligenceProfileConfig()
    try:
        return IntelligenceProfileConfig.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Invalid %s — falling back to defaults: %s", p, exc)
        return IntelligenceProfileConfig()


def save_to_workspace(
    workspace_root: Optional[str], config: IntelligenceProfileConfig
) -> Path:
    """Atomic write (tmp → os.replace) of the config file. Returns the written path.

    Raises WorkspaceRootMissingError if no folder workspace is open — the caller
    in main.py must catch this and surface a user-visible warning rather than
    crashing the WS receive loop.
    """
    p = _config_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, p)
    logger.info(
        "Profile config written: %s (profile=%s, master=%s)",
        p, config.profile, config.master_enabled,
    )
    return p
