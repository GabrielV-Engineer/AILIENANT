import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter

import core.db as catalog_db

router = APIRouter(prefix="/api/v1/system", tags=["system"])

_SETTINGS_PATH = Path.home() / ".ailienant" / "settings.json"
# Phase 7.9.A.7 — scalar-only store. Entity lists (skills/mcp/hooks/role overrides)
# live in the WAL-mode catalog DB, not here, to avoid lost-update races. Scalars are
# still read-modify-write on one file, so a module-level lock serializes mutations.
_DEFAULTS: dict = {
    "analyst_name": "Natt",
    "output_style": "default",       # default | concise | explanatory | code_only
    "permission_mode": "default",    # default | plan | auto  (SessionPermissionMode)
}
_settings_lock = asyncio.Lock()

_OUTPUT_STYLES = {"default", "concise", "explanatory", "code_only"}
_PERMISSION_MODES = {"default", "plan", "auto"}
_HOOK_EVENTS = {"pre_patch", "post_patch"}


def _soul_path() -> Path:
    env = os.environ.get("AILIENANT_SOUL_PATH")
    return Path(env) if env else Path.home() / ".ailienant" / "SOUL.md"


def _read_settings() -> dict:
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    # IOError / PermissionError propagates → endpoint returns 500 (do not silently overwrite)
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def _write_settings(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@router.get("/soul")
async def get_soul() -> dict:
    path = _soul_path()
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"content": content}


@router.post("/soul")
async def save_soul(body: dict) -> dict:
    content = str(body.get("content", ""))
    path = _soul_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True}


@router.get("/settings")
async def get_settings() -> dict:
    async with _settings_lock:
        return _read_settings()


@router.post("/settings")
async def save_settings(body: dict) -> dict:
    async with _settings_lock:
        settings = _read_settings()
        if "analyst_name" in body:
            settings["analyst_name"] = str(body["analyst_name"]).strip() or "Natt"
        if "output_style" in body:
            style = str(body["output_style"]).strip()
            settings["output_style"] = style if style in _OUTPUT_STYLES else "default"
        if "permission_mode" in body:
            mode = str(body["permission_mode"]).strip()
            settings["permission_mode"] = mode if mode in _PERMISSION_MODES else "default"
        _write_settings(settings)
        return settings


# ── Phase 7.9.A.7.d — Hooks (config-capture; execution wiring is a follow-up) ──


@router.get("/hooks")
async def get_hooks() -> dict:
    return {"hooks": await catalog_db.list_hooks()}


@router.post("/hooks")
async def save_hook(body: dict) -> dict:
    event = str(body.get("event", "")).strip()
    command = str(body.get("command", "")).strip()
    if event not in _HOOK_EVENTS or not command:
        return {"ok": False, "error": "event must be pre_patch|post_patch and command non-empty"}
    hook_id = str(body.get("id") or uuid.uuid4().hex)
    enabled = bool(body.get("enabled", True))
    await catalog_db.upsert_hook(hook_id, event, command, enabled)
    return {"ok": True, "hooks": await catalog_db.list_hooks()}


@router.delete("/hooks/{hook_id}")
async def remove_hook(hook_id: str) -> dict:
    await catalog_db.delete_hook(hook_id)
    return {"ok": True, "hooks": await catalog_db.list_hooks()}
