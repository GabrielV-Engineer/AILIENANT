"""Active-project registry endpoint for the dashboard's project selector.

The on-disk analytics stores (catalog graph, telemetry, audit, DLQ) are keyed by
an opaque ``project_id`` hash and never record the workspace path or a name. This
router exposes the one persistent id -> path mapping (``projects`` table) so the
dashboard can render a human-readable selector and re-scope project-aware panels.
"""
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter

from core import db as catalog_db

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects")
async def list_projects() -> List[Dict[str, Any]]:
    """Return the selectable projects, most-recently-seen first.

    Ghost filter: a workspace whose root no longer exists on disk (deleted, or an
    unmounted external/network drive) is omitted so the selector never offers an id
    that would 404 every panel. The registry row is intentionally left in place —
    an absent drive may return — so this is a read-time filter, not a deletion.
    """
    rows = await catalog_db.get_all_projects()
    out: List[Dict[str, Any]] = []
    for project_id, workspace_root, last_seen in rows:
        # O(1) stat per project; the registry is tiny (one row per connected window).
        if not Path(workspace_root).exists():
            continue
        out.append({
            "id": project_id,
            "name": Path(workspace_root).name or workspace_root,
            "path": workspace_root,
            "last_seen": last_seen,
        })
    return out
