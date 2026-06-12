"""Capability handlers for the External Capability Gateway.

Each catalog verb maps to one async handler here. The handlers split along the
architecture's natural seam:

* READ_ONLY verbs (memory and dependency-graph queries) run in-process against the
  shared on-disk stores, keyed by a per-workspace project id. They need no live host.
* The EXECUTE verb and its polling companion reach the running engine over loopback
  HTTP: a task is submitted to the host and its status is read back. These fail fast
  with a clear error when no host is running.

Handlers are exported as a static name->callable mapping that the server registers;
this module imports nothing from the server, so no import cycle can form. Heavy
backend modules are imported lazily inside each handler to keep this import cheap.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List

import httpx

from core.config.host_discovery import HostCoords, resolve_host_or_error
from core.permissions import session_mode_from_frontend
from gateway.governance import INTERNAL_TASK_MODE

logger = logging.getLogger("GATEWAY_HANDLERS")

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]


class InvalidArguments(Exception):
    """Raised by a handler when a required call argument is absent or empty.

    The dispatcher maps it to a top-level ``invalid_arguments`` error envelope, so the
    error surfaces as the call's own status rather than nested inside a success result.
    """

    def __init__(self, missing: List[str]) -> None:
        super().__init__("missing required arguments: " + ", ".join(missing))
        self.missing = missing

# The frontend selector string whose session policy equals the gateway's conservative
# internal-task posture. A gateway-submitted task runs under this mode so the spawned
# agent's mutating actions still gate (and, with no human in an external caller's loop,
# degrade to deny). The explicit guard below pins the wire string to that posture.
_CONSERVATIVE_FRONTEND_MODE = "ask_before_edits"

if session_mode_from_frontend(_CONSERVATIVE_FRONTEND_MODE) is not INTERNAL_TASK_MODE:
    raise RuntimeError(
        "Conservative frontend mode %r no longer maps to the internal-task posture %r; "
        "the run_task wire contract has drifted from the permission engine."
        % (_CONSERVATIVE_FRONTEND_MODE, INTERNAL_TASK_MODE)
    )

# Loopback deadlines. Submit returns a 202 ack immediately; status is a fast read.
_SUBMIT_TIMEOUT_S = 10.0
_STATUS_TIMEOUT_S = 5.0


def project_id_for(workspace_root: str) -> str:
    """Derive the per-workspace project id the on-disk stores are keyed by.

    Mirrors the editor's identity exactly: the SHA-256 hex digest of the raw workspace
    root path. The caller must pass the same absolute path the editor uses, or the
    digest will not match the indexed data.
    """
    return hashlib.sha256(workspace_root.encode("utf-8")).hexdigest()


def _require(args: Dict[str, Any], required: List[str]) -> None:
    """Raise ``InvalidArguments`` if any required key is absent or empty."""
    absent = [key for key in required if not args.get(key)]
    if absent:
        raise InvalidArguments(absent)


# ── READ_ONLY verbs (in-process, host-independent) ────────────────────────────


async def handle_query_memory(args: Dict[str, Any]) -> Any:
    _require(args, ["query", "workspace_root"])
    from core.memory.semantic_memory import SemanticMemoryManager

    project_id = project_id_for(args["workspace_root"])
    pairs = await SemanticMemoryManager().search_snippets(
        args["query"], workspace_hash=project_id
    )
    return [{"file_path": file_path, "snippet": snippet} for file_path, snippet in pairs]


async def handle_get_dependents(args: Dict[str, Any]) -> Any:
    _require(args, ["symbol", "workspace_root"])
    from core import db

    project_id = project_id_for(args["workspace_root"])
    return await db.get_dependents(args["symbol"], project_id)


async def handle_get_workspace_graph(args: Dict[str, Any]) -> Any:
    _require(args, ["workspace_root"])
    from core import db

    project_id = project_id_for(args["workspace_root"])
    edges = await db.get_graph_edges_enriched(project_id)
    return [
        {"source": source, "target": target, "confidence": confidence, "score": score}
        for source, target, confidence, score in edges
    ]


# ── EXECUTE verb + polling companion (loopback to the live host) ──────────────


def _auth_headers(coords: HostCoords) -> Dict[str, str]:
    """Attach the host's loopback token when one is configured."""
    return {"X-AILIENANT-TOKEN": coords.token} if coords.token else {}


async def _submit_task_loopback(
    coords: HostCoords, task_id: str, payload: Dict[str, Any]
) -> None:
    """POST a task to the running host. Raises on transport or HTTP failure."""
    headers = {**_auth_headers(coords), "X-Task-ID": task_id}
    url = f"http://127.0.0.1:{coords.port}/api/v1/task/submit"
    async with httpx.AsyncClient(timeout=_SUBMIT_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


async def _get_status_loopback(coords: HostCoords, task_id: str) -> Any:
    """GET a task's status from the running host. Raises on transport or HTTP failure."""
    url = f"http://127.0.0.1:{coords.port}/api/v1/task/{task_id}/status"
    async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT_S) as client:
        resp = await client.get(url, headers=_auth_headers(coords))
        resp.raise_for_status()
        return resp.json()


async def handle_run_task(args: Dict[str, Any]) -> Any:
    _require(args, ["prompt", "workspace_root"])
    coords = await resolve_host_or_error()
    task_id = uuid.uuid4().hex
    workspace_root = args["workspace_root"]
    payload = {
        "task_prompt": args["prompt"],
        "dirty_buffers": [],
        "project_id": project_id_for(workspace_root),
        "workspace_root": workspace_root,
        "execution_mode": _CONSERVATIVE_FRONTEND_MODE,
        "request_id": uuid.uuid4().hex,
    }
    await _submit_task_loopback(coords, task_id, payload)
    return {"task_id": task_id, "status": "submitted", "poll": "check_task_status"}


async def handle_check_task_status(args: Dict[str, Any]) -> Any:
    _require(args, ["task_id"])
    coords = await resolve_host_or_error()
    return await _get_status_loopback(coords, args["task_id"])


# Static name->callable export. The server imports this and registers from it; this
# module never imports the server, so the dependency graph stays acyclic.
CAPABILITY_HANDLERS: Dict[str, Handler] = {
    "run_task": handle_run_task,
    "check_task_status": handle_check_task_status,
    "query_memory": handle_query_memory,
    "get_dependents": handle_get_dependents,
    "get_workspace_graph": handle_get_workspace_graph,
}
