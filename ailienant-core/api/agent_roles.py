# ailienant-core/api/agent_roles.py
# NOTE: deliberately NOT named api/agents.py — that would shadow the top-level
# `agents/` package and break `from agents.roles import ...`.
"""Phase 7.9.A.7.b — Agents (role system-prompt overrides).

Surfaces the 8 hardcoded RBAC roles from agents/roles.py and lets the user
persist a per-role system-prompt override into the WAL-mode catalog DB.

Config-capture only: applying overrides inside build_coder_system_prompt() is a
tracked follow-up. The orchestrator persona (SOUL.md) + Analyst name remain
editable via /api/v1/system/soul and /api/v1/system/settings.
"""
from fastapi import APIRouter
from typing import Any, Dict

import core.db as catalog_db
from agents.roles import ROLE_REGISTRY, _BASE_CODER_PROMPT

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("/roles")
async def get_roles() -> Dict[str, Any]:
    overrides = await catalog_db.list_agent_overrides()
    roles = [
        {
            "role": role,
            "base_prompt": cfg["system_prompt"],
            "override": overrides.get(role),
            "editable": True,
        }
        for role, cfg in ROLE_REGISTRY.items()
    ]
    return {"base_coder_prompt": _BASE_CODER_PROMPT, "roles": roles}


@router.post("/roles/{role}")
async def save_role(role: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if role not in ROLE_REGISTRY:
        return {"ok": False, "error": f"unknown role {role!r}"}
    prompt = str(body.get("system_prompt", "")).strip()
    if prompt:
        await catalog_db.upsert_agent_override(role, prompt)
    else:
        # Empty override = revert this role to its built-in base prompt.
        await catalog_db.delete_agent_override(role)
    overrides = await catalog_db.list_agent_overrides()
    return {"ok": True, "override": overrides.get(role)}
