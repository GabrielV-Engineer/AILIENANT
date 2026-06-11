# ailienant-core/api/skills.py
"""Phase 7.9.A.7.f — Skills (prompt/command templates).

A "skill" here is a reusable instruction snippet the user can insert into the
prompt bar (insert) or author and save (create). Persisted in the WAL-mode
catalog DB (not settings.json) so concurrent CRUD cannot lose updates.

This is the lightweight Phase-7 template feature; the Phase 9.4 Skills-as-Tools
marketplace is a future superset.
"""
import uuid
from typing import Any, Dict

from fastapi import APIRouter

import core.db as catalog_db

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


@router.get("")
async def list_skills() -> Dict[str, Any]:
    return {"skills": await catalog_db.list_skills()}


@router.post("")
async def save_skill(body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name", "")).strip()
    skill_body = str(body.get("body", "")).strip()
    if not name or not skill_body:
        return {"ok": False, "error": "name and body are required"}
    description = str(body.get("description", "")).strip() or None
    enabled = bool(body.get("enabled", True))
    scope = str(body.get("scope", "global")).strip() or "global"
    if scope not in ("global", "workspace"):
        return {"ok": False, "error": "scope must be 'global' or 'workspace'"}
    workspace_root = str(body.get("workspace_root", "")).strip() or None
    if scope == "workspace" and not workspace_root:
        return {"ok": False, "error": "workspace scope requires a workspace_root"}
    skill_id = str(body.get("id") or uuid.uuid4().hex)
    await catalog_db.upsert_skill(
        skill_id,
        name,
        skill_body,
        description=description,
        enabled=enabled,
        scope=scope,
        workspace_root=workspace_root,
    )
    return {"ok": True, "skills": await catalog_db.list_skills()}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str) -> Dict[str, Any]:
    await catalog_db.delete_skill(skill_id)
    return {"ok": True, "skills": await catalog_db.list_skills()}
