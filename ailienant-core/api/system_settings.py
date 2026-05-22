import json
import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/system", tags=["system"])

_SETTINGS_PATH = Path.home() / ".ailienant" / "settings.json"
_DEFAULTS: dict = {"analyst_name": "Natt"}


def _soul_path() -> Path:
    env = os.environ.get("AILIENANT_SOUL_PATH")
    return Path(env) if env else Path.home() / ".ailienant" / "SOUL.md"


def _read_settings() -> dict:
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    # IOError / PermissionError propagates → endpoint returns 500 (do not silently overwrite)


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
    return _read_settings()


@router.post("/settings")
async def save_settings(body: dict) -> dict:
    settings = _read_settings()
    if "analyst_name" in body:
        settings["analyst_name"] = str(body["analyst_name"]).strip() or "Natt"
    _write_settings(settings)
    return settings
