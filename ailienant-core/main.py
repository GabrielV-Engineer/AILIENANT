import asyncio
import io
import logging
import mimetypes
import os
import secrets
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, cast

# Phase 7.12.9 (Fix 4) — force UTF-8 on stdout/stderr BEFORE anything logs or prints.
# On Windows the default cp1252 console raises 'charmap' codec errors on emoji (📋,
# ⚠️, 🔀, …) used across agent traces, which crashes the node mid-run and mimics a
# Pydantic timeout/retry. Reconfiguring both streams fixes print() and the logging
# StreamHandler (which targets stderr) in one shot.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8")

import httpx

# --- IMPORTACIONES FASE 0 (Transporte y WebSockets) ---
from api.api_contracts import ModelInfo, ModelsAvailableResponse
from api.websocket_manager import (
    vfs_manager,
    register_session_cleanup_hook as _register_session_cleanup_hook,
)
from core.lifecycle_manager import lifecycle_manager

# --- IMPORTACIONES FASE 1.2 (Servicio Cognitivo y VFS) ---
from core import db as catalog_db
from core import benchmark_service
from core import storage_paths
from core.config_generator import discover_models
from core.db_maintenance import WALCheckpointer
from core.sandbox import resolve_default_adapter
from core.task_service import TaskPayload, get_task_service
from core.config.byom_config import stream_watchdog_ms
from core.config.host_discovery import clear_run_state, write_run_state
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse as _JSONResponse, PlainTextResponse
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from shared.config import LITELLM_PROXY_API_KEY, LITELLM_PROXY_BASE_URL

# --- IMPORTACIONES FASE 2 (Persistencia y Mantenimiento) ---
from brain.checkpoint import checkpoint_manager

# --- IMPORTACIONES FASE 6.4 (Dead Letter Queue & Resume API) ---
from brain.engine import alienant_app
from brain.state import AIlienantGraphState
from core.dead_letter import get_pending_dlqs, init_dlq_table, mark_dlq_resolved
from core.audit import init_audit_table  # Phase 6.6 — HITL audit ledger
from core.mcp_registry import init_registry
from tools.mcp_adapter import autoconnect_enabled_mcp_servers, shutdown_mcp_sessions
from shared.logging_filters import SecretsScrubberFilter  # Phase 6.7 — DLP scrubber
from langchain_core.runnables import RunnableConfig

# --- IMPORTACIONES FASE 3.4.5 (MCTS Mirror) ---
from api.mcts_mirror import MergeReport, apply_merge, get_virtual_file

# --- IMPORTACIONES FASE 3.4.7 (Silent Telemetry + Rule Distillation) ---
from agents.analyst import distill_rejection_to_rule
from core.rules import rule_manager

# --- IMPORTACIONES FASE 3.4.8 (Hybrid Cognitive Architecture) ---
from core.token_ledger import token_ledger
from core.telemetry import latency_percentiles, recent_oom_events, recent_routing_decisions
from core.telemetry_log import configure_telemetry_log, shutdown_telemetry_log
from core.observability import configure_langsmith

# --- IMPORTACIONES FASE 3.5 (Memory Janitor) ---
from core.janitor import JanitorReport, run_janitor

# --- IMPORTACIONES FASE 2.3 (Process Pool e Indexing) ---
from core.compute_pool import compute_pool
from brain.memory import _worker_init, calculate_graph_analytics_sync

# --- IMPORTACIONES FASE 2.5 (Lazy Indexer) ---
from core.indexer import lazy_indexer, reactive_indexer, SingleFlightCoordinator
from core.memory_snapshot import export_memory_snapshot, import_memory_snapshot
from brain.daemon import overnight_daemon

# --- IMPORTACIONES FASE 7.9.B.1 (Memory Dashboard REST surface) ---
from api.memory_dashboard import router as memory_router

# --- IMPORTACIONES FASE 7.9.B.2 (BYOM Models REST surface) ---
from api.byom import router as byom_router

# --- IMPORTACIONES FASE 7.9.B.3 (Hardware Monitor REST surface) ---
from api.hardware import router as hardware_router, _get_profile as _get_hw_profile
from core.execution_mode import get_mode as get_execution_mode_pref

# --- IMPORTACIONES FASE 7.9.B.7 (Runtime/Environment REST surface) ---
from api.runtime import router as runtime_router

# --- IMPORTACIONES FASE 7.9.B.4 + 7.9.B.5 (System Settings + Audit REST surface) ---
from api.system_settings import router as system_settings_router
from api.audit import router as audit_router
from api.projects import router as projects_router

# --- IMPORTACIONES FASE 7.9.A.7 (Command-menu backends: agents/mcp/skills) ---
from api.agent_roles import router as agents_router
from api.mcp_servers import router as mcp_router
from api.skills import router as skills_router

# Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel Debugging REST surface
from api.sessions import router as sessions_router

# --- IMPORTACIONES FASE 2.6 (I/O Coalescer) ---
from core.io_coalescer import io_coalescer, is_critical_file, _UNLINK_SENTINEL
from shared.contracts import (
    PPRRequest, PPRResult,
)
from api.api_contracts import DirtyBuffer
from core.vfs_middleware import DirtyBuffer as VfsDirtyBuffer
from api.ws_contracts import IdeTelemetryPayload, DreamingRunPayload

# Phase 7.9.A.5.1 — ephemeral auth token + dynamic port injected by the extension.
# When AILIENANT_AUTH_TOKEN is absent (manual backend start), auth middleware is bypassed.
# Defined after the import block so module-level imports stay at the top (ruff E402).
_AUTH_TOKEN: Optional[str] = os.environ.get("AILIENANT_AUTH_TOKEN") or None
_API_PORT: int = int(os.environ.get("AILIENANT_API_PORT", "8000"))

# Configuración centralizada de observabilidad
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AILIENANT_API")


# =====================================================================
# LIFESPAN — Startup & Graceful Shutdown
# =====================================================================

def _publish_host_discovery() -> None:
    """Write the loopback coordinates the External Capability Gateway reads.

    Port and token are resolved fresh from the environment the server is actually
    running under — the extension sets ``AILIENANT_API_PORT`` equal to the uvicorn
    ``--port`` it binds, so the env is the authoritative value, not a stale global.
    A bare default port with no env var set means a manual/standalone start, where
    discovery may not match the real bind; we warn rather than silently mislead.
    """
    env_port = os.environ.get("AILIENANT_API_PORT")
    port = int(env_port) if env_port else _API_PORT
    token = os.environ.get("AILIENANT_AUTH_TOKEN") or None
    if env_port is None:
        logger.warning(
            "AILIENANT_API_PORT unset; host-discovery advertises default port %d "
            "which may not match the real bind.",
            port,
        )
    try:
        write_run_state(port, token, os.getpid())
    except OSError as exc:  # noqa: BLE001 — discovery is best-effort, never blocks startup
        logger.warning("Could not write host-discovery state: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── Startup ──────────────────────────────────────────────────────────
    # Phase 6.7 — install the DLP secrets scrubber. The filter is attached to
    # the root logger AND to every root handler: handler-level filtering is what
    # redacts records propagated from named child loggers (AUDIT, SUPERVISOR…).
    _scrubber = SecretsScrubberFilter()
    _root_logger = logging.getLogger()
    _root_logger.addFilter(_scrubber)
    for _handler in _root_logger.handlers:
        _handler.addFilter(_scrubber)

    await resolve_default_adapter()          # Phase 6.1.4 — bind sandbox tier
    # Inject the concrete WS host bridge for the trusted devcontainer tier from
    # the composition root, so core depends only on the HostExecutionBridge
    # abstraction it owns and never imports the transport layer.
    from api.devcontainer_bridge import WebSocketHostBridge
    from core.sandbox import set_trusted_bridge
    set_trusted_bridge(WebSocketHostBridge())
    await catalog_db.init_db()
    await init_dlq_table()                   # Phase 6.4 — dead_letter_tasks table
    await init_audit_table()                 # Phase 6.6 — hitl_audit_log ledger
    init_registry()                          # curated regulated-server tier overrides
    await autoconnect_enabled_mcp_servers()  # connect enabled MCP servers once per host lifecycle
    checkpoint_manager.initialize()          # WAL pragmas applied once here
    compute_pool.initialize(initializer=_worker_init)
    io_coalescer.register_dispatch(_dispatch_indexing_and_ppr)
    io_coalescer.register_mass_handler(_handle_mass_change)
    _wal = WALCheckpointer(checkpoint_manager)
    _wal.start()
    overnight_daemon.start()                 # on-demand memory consolidation (no timer)
    _publish_host_discovery()                # advertise loopback coords for the gateway
    configure_langsmith()                    # opt-in tracing; no-op unless env-configured
    logger.info("🟢 AILIENANT startup complete (WAL mode active).")

    yield  # application handles requests

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("🔴 AILIENANT shutdown initiated.")

    # 1. Stop accepting new WebSocket connections
    vfs_manager.shutting_down = True

    # 2. Drain in-flight agent tasks (10s grace period)
    pending = list(vfs_manager.active_tasks)
    if pending:
        logger.info("Draining %d in-flight task(s) (timeout=10s)...", len(pending))
        await asyncio.wait(pending, timeout=10.0)

    # 2b. Cancel any in-flight consolidation passes before tearing down the daemon.
    for _dream in list(_dreaming_tasks.values()):
        if not _dream.done():
            _dream.cancel()
    await overnight_daemon.stop()

    # 3. Flush all in-memory L1 sessions to L2 before WAL truncate (Phase 2.2.B)
    checkpoint_manager.flush_all_sessions()
    await catalog_db.wal_checkpoint()

    # 4. Stop WAL worker, force-truncate, then close the connection
    await _wal.stop()
    await _wal.force_truncate()
    checkpoint_manager.close()
    logger.info("🧹 SQLite connection closed — WAL/SHM files released.")
    compute_pool.shutdown(wait=True, cancel_futures=True)
    logger.info("🧹 Compute pool shut down — no orphan processes.")
    await shutdown_mcp_sessions()  # close MCP stdio sessions so children never outlive the host
    clear_run_state()  # remove the host-discovery file so a crash leaves a detectable stale one
    shutdown_telemetry_log()  # drain the queue, join the listener thread, close the file


# =====================================================================
# APPLICATION
# =====================================================================

app = FastAPI(
    title="AILIENANT API Gateway",
    description="Backend bicefálico con VFS Middleware O(1)",
    version="1.0.0",
    lifespan=lifespan,
)

# SecOps: CORS es crítico para el Webview (vscode-webview://).
# Phase 7.9.A.5.1: replaced wildcard with explicit allowlist.
# vscode-webview:// sub-origins change per panel — regex required.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"vscode-webview://[a-z0-9]+",
    allow_origins=[
        f"http://localhost:{_API_PORT}",
        f"http://127.0.0.1:{_API_PORT}",
        "http://localhost",
        "http://127.0.0.1",
        "null",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Phase 7.9.A.5.1 — HTTP auth middleware.
# Validates X-AILIENANT-TOKEN (or Authorization: Bearer) on every non-health request
# when AILIENANT_AUTH_TOKEN env var is set. Dev-mode bypass: if the var is absent,
# all requests pass. Dashboard SPA (same-origin) is exempt: S7-D CSRF already guards it.
@app.middleware("http")
async def _require_token(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if not _AUTH_TOKEN:
        return await call_next(request)          # dev mode: no token configured
    if request.url.path == "/":
        return await call_next(request)          # health check: always public

    # Dashboard static files: direct browser navigation sends no Origin header.
    if request.url.path.startswith("/dashboard"):
        return await call_next(request)

    # Dashboard SPA API calls: same-origin GETs carry Referer but not Origin.
    referer = request.headers.get("referer", "")
    if referer.startswith((
        f"http://localhost:{_API_PORT}/dashboard",
        f"http://127.0.0.1:{_API_PORT}/dashboard",
    )):
        return await call_next(request)

    origin = request.headers.get("origin", "")
    # Dashboard SPA at http://localhost:{port} cannot receive VS Code postMessage
    # so it cannot obtain the token. S7-D CSRF on mutation endpoints guards it.
    if origin in (
        f"http://localhost:{_API_PORT}",
        f"http://127.0.0.1:{_API_PORT}",
        "null",
    ):
        return await call_next(request)

    raw = (
        request.headers.get("X-AILIENANT-TOKEN", "")
        or request.headers.get("authorization", "").removeprefix("Bearer ")
    )
    # secrets.compare_digest: constant-time comparison — timing attacks are feasible
    # on localhost where network jitter cannot mask micro-second differences.
    if not raw or not secrets.compare_digest(raw, _AUTH_TOKEN):
        return _JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# The dashboard SPA loads its code-split chunks via dynamic import(); a browser
# refuses to execute an ES module served with a non-JavaScript content type.
# Windows' registry-backed mimetypes can map .js/.mjs to text/plain, so register
# the correct types explicitly before mounting the static files.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

# Dashboard SPA — served at /dashboard
_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "ailienant-extension", "dist", "dashboard")
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/dashboard", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")

# Phase 7.9.B.1 — Memory dashboard REST endpoints (sections/graph/vectors)
app.include_router(memory_router)

# Phase 7.9.B.2 — BYOM Models REST endpoints (test/config)
app.include_router(byom_router)

# Phase 7.9.B.3 — Hardware Monitor REST endpoints (profile/mode)
app.include_router(hardware_router)

# Phase 7.9.B.7 — Runtime/Environment REST endpoints (status/start-docker)
app.include_router(runtime_router)

# Phase 7.9.B.4 — System Settings (SOUL.md + analyst name)
app.include_router(system_settings_router)

# Phase 7.9.B.5 — Audit Ledger REST endpoints (log/stats/verify)
app.include_router(audit_router)

# Active-project registry — GET /api/v1/projects for the dashboard selector
app.include_router(projects_router)

# Phase 7.9.A.7 — Command-menu backends (agent role overrides, MCP registry, skills)
app.include_router(agents_router)
app.include_router(mcp_router)
app.include_router(skills_router)

# Phase 7.11.8 — Time-travel: GET /api/v1/sessions/{thread_id}/checkpoints
app.include_router(sessions_router)

# Service layer instance (dependency injection). Sourced from the shared
# singleton accessor so in-process tools resolve the same instance the host wires.
task_service = get_task_service()

# Phase 7.11.6 — wire TaskService.cleanup_session into the WS disconnect path
# so the Rich Tool Chips registry is purged when a client drops. The hook bus
# lives in api.websocket_manager to avoid the circular import that bites if
# the manager imports core.task_service eagerly.
_register_session_cleanup_hook(task_service.cleanup_session)

# Cancel a session's in-flight generation runner when its socket drops. Without
# this a client that closes mid-stream leaves the runner alive, broadcasting
# tokens into a dead connection until it finishes — a zombie task that the
# event-driven Push model makes routine (tab switches, reloads). abort_session
# is idempotent and a no-op once the turn has completed and auto-deregistered.
_register_session_cleanup_hook(task_service.abort_session)

# Session-scoped planner mode registry — read by TaskService when LangGraph is wired (Phase 2)
planner_mode_registry: Dict[str, bool] = {}

# PPR debounce registry — one asyncio.Task per project_id, cancelled and replaced on each file save
_ppr_tasks: Dict[str, asyncio.Task[None]] = {}
_PPR_DEBOUNCE_S: float = 2.0

# Reactive re-index single-flight: at most one in-flight pass per (project, file),
# with a trailing re-run so the freshest save always wins. Collapses overlapping
# re-index when a compute-pool run outlives the next debounce window.
_reindex_singleflight = SingleFlightCoordinator()

# Fire-and-forget task runners (Phase 7.9.B.17). Strong refs prevent the event
# loop from GC-ing an in-flight submit before the agent pipeline finishes.
_task_submit_tasks: set[asyncio.Task[Any]] = set()

# Workspace registry — populated on client_workspace_init; used by mass change handler
_workspace_registry: Dict[str, str] = {}  # project_id → workspace_root
_session_workspace_root: Dict[str, str] = {}  # client_id → workspace_root (Phase 3.4.1)
_session_workspace_pid: Dict[str, int] = {}  # client_id → workspace_pid (Phase 4.4)
_session_project_id: Dict[str, str] = {}  # client_id → project_id (reactive-index routing)

# Per-submit idempotency cache — a bounded TTL map of recently-seen request_ids
# so a resubmit (e.g. driven by a WS reconnect) never spawns a second
# generation. Bounded in both time and size; O(1) amortized.
_RECENT_REQUEST_TTL_S: float = 120.0
_RECENT_REQUEST_CAP: int = 256
_recent_request_ids: "OrderedDict[str, float]" = OrderedDict()


def _is_duplicate_request(request_id: str) -> bool:
    """Record ``request_id`` and report whether it was already seen within the TTL.

    First sighting records and returns False; a repeat inside the window returns
    True. Expired entries (oldest first) are pruned on every call, and the map is
    hard-capped so a long-lived process can never leak memory here.
    """
    now = time.monotonic()
    while _recent_request_ids:
        oldest_id, ts = next(iter(_recent_request_ids.items()))
        if now - ts > _RECENT_REQUEST_TTL_S:
            _recent_request_ids.pop(oldest_id, None)
        else:
            break
    if request_id in _recent_request_ids:
        return True
    _recent_request_ids[request_id] = now
    while len(_recent_request_ids) > _RECENT_REQUEST_CAP:
        _recent_request_ids.popitem(last=False)
    return False

# Manual Dreaming — at most one consolidation per project (a new run cancels the
# prior one). The epoch is a monotonic per-project save counter: the OCC anchor a
# consolidation captures at start and the daemon re-checks before committing, so a
# save landing mid-run invalidates the snapshot and aborts the write.
_dreaming_tasks: Dict[str, asyncio.Task[None]] = {}
_dreaming_epoch: Dict[str, int] = {}


def _abort_dreaming(project_id: str) -> None:
    """Cancel an in-flight consolidation and bump the project's save epoch.

    Called on every save/telemetry frame: the bump invalidates any snapshot a
    running pass captured, and the cancel stops the LLM call so the daemon never
    fights a resuming typist.
    """
    if not project_id:
        return
    _dreaming_epoch[project_id] = _dreaming_epoch.get(project_id, 0) + 1
    task = _dreaming_tasks.get(project_id)
    if task is not None and not task.done():
        task.cancel()


def _trigger_dreaming(client_id: str, focus_area: Optional[str]) -> None:
    """Spawn a consolidation pass for the session's project, one at a time."""
    project_id = _session_project_id.get(client_id, "")
    workspace_root = _workspace_registry.get(project_id, "")

    prior = _dreaming_tasks.pop(project_id, None)
    if prior is not None and not prior.done():
        prior.cancel()

    epoch_at_start = _dreaming_epoch.get(project_id, 0)

    def _is_stale() -> bool:
        return _dreaming_epoch.get(project_id, 0) != epoch_at_start

    async def _run() -> None:
        try:
            await overnight_daemon.run_consolidation(
                project_id,
                focus_area,
                workspace_root=workspace_root,
                session_id=f"dream:{client_id}",
                stale_check=_is_stale,
            )
        except asyncio.CancelledError:
            logger.info("[Session: %s] Dreaming cancelled (save mid-run).", client_id)
            raise
        except Exception as exc:  # noqa: BLE001 — a failed dream never crashes the loop
            logger.error("[Session: %s] Dreaming failed: %s", client_id, exc)

    task: asyncio.Task[None] = asyncio.create_task(_run(), name=f"dream:{project_id}")
    _dreaming_tasks[project_id] = task

    def _evict(done: "asyncio.Task[None]") -> None:
        # Only drop the slot if it still points at this task — a newer pass may
        # have already replaced it (one consolidation per project).
        if _dreaming_tasks.get(project_id) is done:
            _dreaming_tasks.pop(project_id, None)

    task.add_done_callback(_evict)


# =====================================================================
# ENDPOINTS
# =====================================================================

@app.get("/")
async def health_check() -> dict[str, str]:
    """Endpoint HTTP tradicional para verificar que el servidor está vivo."""
    return {"status": "online", "phase": "2.4", "system": "Tiered Checkpoint + PPR Active"}


@app.get("/api/v1/models/available", response_model=ModelsAvailableResponse)
async def get_available_models() -> ModelsAvailableResponse:
    """
    Phase 1.6.3 — Model discovery endpoint.

    Strategy:
    1. Try LiteLLM proxy GET /model/info for a live, authoritative model list.
    2. If the proxy is unreachable, fall back to direct port scan via ConfigGenerator
       so the endpoint works even before LiteLLM is started for the first time.
    """
    litellm_available = False
    models: List[ModelInfo] = []

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                f"{LITELLM_PROXY_BASE_URL}/model/info",
                headers={"Authorization": f"Bearer {LITELLM_PROXY_API_KEY}"},
            )
            if resp.status_code == 200:
                litellm_available = True
                for entry in resp.json().get("data", []):
                    alias = entry.get("model_name", "")
                    params = entry.get("litellm_params", {})
                    real_model: str = params.get("model", alias)
                    provider = real_model.split("/")[0] if "/" in real_model else "cloud"
                    models.append(ModelInfo(
                        id=alias,
                        name=real_model,
                        provider=provider,
                        is_local=provider in ("ollama", "lmstudio"),
                    ))
                logger.info("Model discovery via LiteLLM proxy: %d model(s)", len(models))
    except Exception:
        pass  # proxy not running — fall through to direct scan

    if not litellm_available:
        raw = await discover_models()
        models = [ModelInfo(**m) for m in raw]
        logger.info("Model discovery via direct scan: %d model(s)", len(models))

    return ModelsAvailableResponse(models=models, litellm_available=litellm_available)


@app.post("/api/v1/task/submit", status_code=202)
async def submit_task(
    payload: TaskPayload,
    x_task_id: str = Header(
        ..., alias="X-Task-ID"
    ),  # Trazabilidad desde el frontend TS
) -> Dict[str, object]:
    """Dispatch a cognitive mission and return immediately (202 Accepted).

    The agent pipeline (planner + coder, or the streaming chat completion) can run
    for far longer than the client's HTTP timeout, so the request must NOT block on
    it. We schedule process_task in the background and stream every result over the
    WebSocket; the HTTP response only acknowledges receipt. Errors inside the runner
    are surfaced over the WS (actionable token + stream_end) so the UI never hangs.
    """

    # Phase 7.12.9 (Fix 3) — the frontend now sends workspace_root, but if an
    # older client omits it, fall back to the live root captured at
    # client_workspace_init (keyed by client_id == x_task_id). This guarantees
    # the Planner/GraphRAG use the dynamic root, never an empty/stale value.
    if not payload.workspace_root:
        _fallback_root = _session_workspace_root.get(x_task_id, "")
        if _fallback_root:
            payload.workspace_root = _fallback_root

    # Guarantee a project_id on the payload so the per-project telemetry/audit/DLQ
    # tags are never NULL for a normally-submitted task. An older client (or the
    # HTTP-only path) may omit it; derive it from the workspace root via the same
    # canonical hash the editor and on-disk stores agree on.
    if not payload.project_id and payload.workspace_root:
        payload.project_id = storage_paths.project_id_for(payload.workspace_root)

    # The WS planner-mode toggle is stored per-session in planner_mode_registry
    # (client_id == x_task_id); fold it into the payload so the coding path can
    # route to the Socratic ideation loop. Without this read the flag was always
    # the default False and every coding turn fell through to the autonomous
    # planner. The `in` guard means an HTTP-only client that never toggled keeps
    # its body-supplied value untouched.
    if x_task_id in planner_mode_registry:
        payload.planner_mode_active = planner_mode_registry[x_task_id]

    # The execution-mode selector governs the session permission policy (mapped
    # to session_permission_mode when the graph state is seeded). Plan mode
    # additionally routes the turn into the Socratic ideation loop, so it must
    # also raise planner_mode_active — the read-only stance and the questioning
    # stance go together. Auto/Ask leave the planner flag as resolved above.
    from core.permissions import SessionPermissionMode, session_mode_from_frontend
    if session_mode_from_frontend(payload.execution_mode) is SessionPermissionMode.PLAN_ONLY:
        payload.planner_mode_active = True

    # Idempotent submit: a duplicate request_id (a reconnect-driven resubmit, or
    # two windows racing) is acknowledged WITHOUT spawning a second runner.
    if payload.request_id and _is_duplicate_request(payload.request_id):
        logger.info(
            "Duplicate submit ignored [task=%s request_id=%s]",
            x_task_id, payload.request_id,
        )
        return {
            "status": "duplicate_ignored",
            "session_id": x_task_id,
            "stream_watchdog_ms": stream_watchdog_ms(),
        }

    async def _runner() -> None:
        try:
            # Resolve execution mode — Zero-Trust hardware gate (Phase 7.9.B.3)
            hw = await _get_hw_profile()
            pref = get_execution_mode_pref()
            if pref == "AUTO":
                resolved_mode = hw.suggested_mode
            elif pref == "FULL_SWARM" and hw.suggested_mode in ("SEQUENTIAL", "MICRO_SWARM"):
                resolved_mode = hw.suggested_mode   # hardware degraded; silent downgrade
            elif pref == "MICRO_SWARM" and hw.suggested_mode == "SEQUENTIAL":
                resolved_mode = "SEQUENTIAL"
            else:
                resolved_mode = pref

            await task_service.process_task(
                session_id=x_task_id, payload=payload, execution_mode=resolved_mode
            )
        except Exception as exc:  # noqa: BLE001 — a background failure must reach the UI, not vanish
            logger.error("Fallo crítico en el motor cognitivo: %s", exc, exc_info=True)
            try:
                await vfs_manager.broadcast_token(
                    x_task_id,
                    "AILIENANT hit an internal error processing this request. "
                    "Check the core logs; if a model is configured, make sure its "
                    "engine is reachable.",
                )
                await vfs_manager.broadcast_stream_end(x_task_id)
            except Exception:  # noqa: BLE001 — never raise out of the background runner
                pass

    _t = asyncio.create_task(_runner(), name=f"task_submit:{x_task_id}")
    # Register the runner with the abort mesh synchronously, before returning the ack,
    # so an immediate status poll observes the task as running rather than missing it in
    # the gap before the background coroutine first executes. `_t` is the runner task
    # itself (never the WS receive loop), so cancelling it propagates CancelledError into
    # the generation coroutine without disturbing the socket; the done-callback inside
    # register_active_task clears the entry on completion.
    task_service.register_active_task(x_task_id, _t)
    _task_submit_tasks.add(_t)
    _t.add_done_callback(_task_submit_tasks.discard)

    # The client arms its stream watchdog from this backend-governed value (longer
    # for slow local engines, tighter for fast cloud APIs) — never a hardcoded UI
    # constant.
    return {
        "status": "accepted",
        "session_id": x_task_id,
        "stream_watchdog_ms": stream_watchdog_ms(),
    }


@app.get("/api/v1/task/{task_id}/status")
async def task_status(task_id: str) -> Dict[str, Any]:
    """Report a submitted task's lifecycle status from existing engine state.

    Serves the gateway's poll-pair companion: a running task is read from the in-flight
    registry, a finished one from its persisted checkpoint chain. A pure read over state
    that already exists — it introduces no new task store.
    """
    return task_service.get_task_status(task_id)


class BenchmarkSubmitPayload(BaseModel):
    """Body for a benchmark submission — an optional frozen-corpus suite name."""

    suite: str = "v1"


@app.post("/api/v1/benchmark/submit", status_code=202)
async def submit_benchmark(
    payload: BenchmarkSubmitPayload,
    x_task_id: str = Header(..., alias="X-Task-ID"),
) -> Dict[str, object]:
    """Start a benchmark run and return immediately; the report is read back later.

    Mirrors task submit: the heavy run is scheduled in the background and the HTTP
    response only acknowledges receipt. A single-flight slot is reserved before the
    task is spawned, and the slot is released exactly once by the spawned task's
    done-callback — so a failure to spawn or register can never strand the slot.
    """
    if not benchmark_service.try_reserve(payload.suite):
        return {"status": "busy", "task_id": x_task_id}

    try:
        runner = asyncio.create_task(
            benchmark_service.run_benchmark(x_task_id, payload.suite),
            name=f"benchmark_submit:{x_task_id}",
        )
    except BaseException:
        # The task never existed, so its done-callback can never fire — release here.
        benchmark_service.release_flight()
        raise

    # The task's done-callback is the sole releaser of the reserved slot from here.
    runner.add_done_callback(lambda _t: benchmark_service.release_flight())

    try:
        # Register synchronously before the ack so an immediate status poll observes
        # the run as in flight; its own done-callback auto-deregisters on completion.
        task_service.register_active_task(x_task_id, runner)
    except BaseException:
        # The slot is now owned by the task's callback; cancel it so the task ends
        # and that callback performs the single release.
        runner.cancel()
        raise

    return {"status": "accepted", "task_id": x_task_id}


@app.get("/api/v1/benchmark/{task_id}/report")
async def benchmark_report(task_id: str) -> Dict[str, Any]:
    """Return a benchmark run's status and, when complete, its report (pure read)."""
    return benchmark_service.read_report(task_id)


# =====================================================================
# PHASE 6.4 — Resume API (Dead Letter Queue rehydration)
# =====================================================================


@app.post("/api/v1/task/resume/{task_id}")
async def resume_task(task_id: str) -> Dict[str, object]:
    """Phase 6.4 — re-hydrate the latest L2 checkpoint of a crashed task and resume.

    Idempotent: a task with no unresolved DLQ episode (never crashed, or already
    resumed to completion) returns ``resumed: false`` without mutating state.
    On success the episode is stamped ``resolved_at`` so a repeated call no-ops.
    """
    pending = await get_pending_dlqs(task_id)
    if not pending:
        return {"resumed": False, "reason": "no_dlq_episode"}

    episode = pending[0]  # newest unresolved
    await checkpoint_manager.arecover(episode.thread_id)  # seed L1 from L2 (offloaded sqlite I/O)
    config: RunnableConfig = {"configurable": {"thread_id": episode.thread_id}}
    # Partial-state update merged into the resumed checkpoint; cast satisfies the
    # ainvoke() overload (full state lives in the L2 checkpoint being resumed).
    resume_input = cast(
        AIlienantGraphState, {"dead_letter_episode_id": episode.episode_id}
    )
    try:
        await alienant_app.ainvoke(resume_input, config=config)
    except Exception as exc:
        logger.error(
            "Resume re-invocation failed [task=%s episode=%s]: %s",
            task_id, episode.episode_id, exc,
        )
        raise HTTPException(status_code=500, detail="Resume re-invocation failed")

    await mark_dlq_resolved(episode.episode_id)
    logger.info(
        "Task resumed [task=%s] from episode=%s (failed_node=%s)",
        task_id, episode.episode_id, episode.failed_node,
    )
    return {
        "resumed": True,
        "from_episode": episode.episode_id,
        "node_resumed_at": episode.failed_node,
    }


@app.get("/api/v1/dlq/pending")
async def list_pending_dlqs(
    task_id: Optional[str] = None, project_id: Optional[str] = None
) -> Dict[str, object]:
    """Report unresolved DLQ episodes for the sidebar Resume affordance.

    Backend surface for the extension's "Resume Task" item and the dashboard's
    recovery panel. Optionally scoped to a ``task_id`` and/or ``project_id``;
    newest episodes first. Read-only — no state mutation.
    """
    pending = await get_pending_dlqs(task_id, project_id)
    return {
        "count": len(pending),
        "episodes": [r.model_dump() for r in pending],
    }


# =====================================================================
# PHASE 3.4.5 — MCTS Mirror endpoints
# =====================================================================


class ApplyMergeRequest(BaseModel):
    workspace_root: str


@app.get("/api/v1/mcts/{node_id}/vfs", response_class=PlainTextResponse)
async def http_get_virtual_file(node_id: str, path: str) -> PlainTextResponse:
    """Read a file out of an MCTS node's vfs_view (RAM CAS + disk fallback)."""
    content = get_virtual_file(node_id, path)
    if content is None:
        raise HTTPException(status_code=404, detail="node or path not found")
    return PlainTextResponse(content)


@app.post("/api/v1/mcts/{node_id}/merge")
async def http_apply_merge(node_id: str, body: ApplyMergeRequest) -> MergeReport:
    """Apply a stable MCTS node's vfs_view to disk; prune the branch."""
    return apply_merge(node_id, body.workspace_root)


# =====================================================================
# PHASE 3.4.7 — Silent Rejection Telemetry
# =====================================================================


class RejectTelemetryPayload(BaseModel):
    uri: str
    original_ai_code: str
    current_user_code: str
    timestamp: float
    workspace_root: str


@app.post("/api/v1/telemetry/reject")
async def http_telemetry_reject(payload: RejectTelemetryPayload) -> Dict[str, object]:
    """Receive AI_PAYLOAD_REJECTED; distill a rule; persist to local .ailienant.json."""
    rule = await distill_rejection_to_rule(
        payload.original_ai_code, payload.current_user_code,
    )
    appended: bool = False
    if rule is not None:
        appended = rule_manager.append_local_rule(payload.workspace_root, rule)
    logger.info(
        "telemetry/reject: uri=%s rule=%r appended=%s",
        payload.uri, rule, appended,
    )
    return {"distilled": rule is not None, "rule": rule, "appended": appended}


# =====================================================================
# PHASE 3.4.8 — Hybrid Cognitive Architecture token telemetry
# =====================================================================


@app.get("/api/v1/telemetry/tokens")
async def http_telemetry_tokens() -> Dict[str, float]:
    """Phase 3.4.8 — return the TokenLedger snapshot (local vs cloud + savings)."""
    return token_ledger.snapshot()


@app.get("/api/v1/telemetry/routing")
async def http_telemetry_routing(
    limit: int = 50, offset: int = 0, project_id: Optional[str] = None
) -> Dict[str, object]:
    """Read recent routing decisions for the dashboard.

    Pagination is clamped server-side (S4+S6) and ``reason`` is secret-masked
    (S1) inside the helper, so this read path is safe to expose read-only. When
    ``project_id`` is supplied the result is scoped to that project.
    """
    return {"decisions": recent_routing_decisions(limit=limit, offset=offset, project_id=project_id)}


@app.get("/api/v1/telemetry/oom")
async def http_telemetry_oom(
    limit: int = 50, offset: int = 0, project_id: Optional[str] = None
) -> Dict[str, object]:
    """Read recent OOM rescue-swap events for the dashboard, optionally project-scoped."""
    return {"events": recent_oom_events(limit=limit, offset=offset, project_id=project_id)}


@app.get("/api/v1/telemetry/latency")
async def http_telemetry_latency(project_id: Optional[str] = None) -> Dict[str, object]:
    """Request-latency P50/P95/P99 summary for the dashboard, optionally project-scoped.

    Percentiles are computed over a bounded most-recent window inside the helper,
    so this read never scans the full ledger. Empty/uninitialised → zeros.
    """
    return latency_percentiles(project_id=project_id)


# =====================================================================
# PHASE 7.1 — Session title auto-generation
# =====================================================================

class TitleGenerateRequest(BaseModel):
    prompt: str
    max_words: int = 5


class TitleGenerateResponse(BaseModel):
    title: str


@app.post("/api/v1/title/generate", response_model=TitleGenerateResponse)
async def http_generate_title(req: TitleGenerateRequest) -> TitleGenerateResponse:
    """Summarize the user's first prompt into a 3–5 word session title.

    Uses the small-tier model via LLMGateway. Falls back to a truncated prompt
    on any error so the UI never hangs on this fire-and-forget call.
    """
    from tools.llm_gateway import LLMGateway
    from shared.config import MODEL_SMALL

    fallback = req.prompt.strip()[:30].rstrip() + ("…" if len(req.prompt.strip()) > 30 else "")
    try:
        sys_prompt = (
            f"Summarize this user request into a {req.max_words}-word title. "
            "No punctuation. No quotes. Output only the title in title case."
        )
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": req.prompt[:400]},
            ],
            model=MODEL_SMALL,
            temperature=0.0,
            max_tokens=24,
            timeout=15.0,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return TitleGenerateResponse(title=fallback)
        raw = getattr(choices[0].message, "content", "") or ""
        title = " ".join(raw.strip().strip('"\'').split())[:60]
        return TitleGenerateResponse(title=title or fallback)
    except Exception as exc:
        logger.warning("title/generate fallback (%s): %s", type(exc).__name__, exc)
        return TitleGenerateResponse(title=fallback)


# =====================================================================
# PHASE 3.5 — Memory Janitor
# =====================================================================

class JanitorRequest(BaseModel):
    workspace_root: str
    retention_days: int = 30


@app.post("/api/v1/system/janitor")
async def http_janitor(payload: JanitorRequest) -> JanitorReport:
    """Phase 3.5 — Trigger Memory Janitor: orphaned vector GC + obsolete graph purge."""
    return await run_janitor(
        workspace_root=payload.workspace_root,
        retention_days=payload.retention_days,
    )


async def _run_ppr_for_project(project_id: str) -> None:
    """Debounced graph-analytics worker: waits 2s after the last save, then dispatches
    degree centrality + Louvain communities + edge confidence to the pool in one build."""
    await asyncio.sleep(_PPR_DEBOUNCE_S)
    try:
        edges_raw = await catalog_db.get_all_edges(project_id)
        if not edges_raw:
            return
        indexed = await catalog_db.get_all_indexed_files()
        indexed_files = tuple(fp for fp, pid in indexed if pid == project_id)
        req = PPRRequest(
            edges=tuple((s, t) for s, t in edges_raw),
            indexed_files=indexed_files,
        )
        result: PPRResult = await compute_pool.run(calculate_graph_analytics_sync, req)
        if result.success and result.scores:
            await catalog_db.upsert_ppr_scores(result.scores, project_id, result.communities)
            if result.edge_confidence:
                await catalog_db.upsert_edge_confidence(
                    list(result.edge_confidence), project_id
                )
            logger.info(
                "Graph analytics for project '%s': %d files scored, %d communities",
                project_id, len(result.scores), len(set(result.communities.values())),
            )
        elif not result.success:
            logger.warning("Graph analytics failed for project '%s': %s", project_id, result.error)
    except asyncio.CancelledError:
        pass  # debounced away — a newer file save superseded this run


def _schedule_ppr(project_id: str) -> None:
    """Cancel any pending PPR task for project_id and schedule a fresh debounced one."""
    existing = _ppr_tasks.get(project_id)
    if existing is not None and not existing.done():
        existing.cancel()
    _ppr_tasks[project_id] = asyncio.create_task(
        _run_ppr_for_project(project_id),
        name=f"ppr:{project_id}",
    )


async def _handle_mass_change(project_id: str) -> None:
    """Triggered by IOCoalescer when a mass event (>100 files) is detected (branch switch)."""
    workspace_root = _workspace_registry.get(project_id, "")
    if not workspace_root:
        logger.warning("Mass change handler: no workspace_root for project_id=%s", project_id)
        return
    lazy_indexer._is_complete = False  # force re-index after branch switch
    await lazy_indexer.start(
        workspace_root=workspace_root,
        project_id=project_id,
        session_id="mass_change",
    )
    logger.info("Mass change: re-triggered lazy indexer for project %s", project_id)


async def _reindex_one(filepath: str, content: str, project_id: str) -> None:
    """Adapter onto the unified reactive entry: resolve workspace_root, delegate.

    Delete (unlink sentinel) purges graph + vector; a write runs the idempotent,
    content-hash-gated index that updates both the dependency graph and the vector
    store under the real project_id, debouncing the analytics pass only when edges
    actually changed.
    """
    workspace_root = _workspace_registry.get(project_id, "")
    if content == _UNLINK_SENTINEL:
        await reactive_indexer.purge(filepath, project_id, workspace_root)
        return
    await reactive_indexer.index(
        filepath,
        content,
        project_id,
        workspace_root,
        on_deps_changed=lambda: _schedule_ppr(project_id),
    )


async def _dispatch_indexing_and_ppr(filepath: str, content: str, project_id: str) -> None:
    """Reactive re-index entry: single-flight per (project, file).

    A NUL-joined key cannot collide on real paths/project ids. The factory rebinds
    the latest ``content`` on each call so a trailing re-run indexes the freshest
    save, never a superseded one.
    """
    key = f"{project_id}\x00{filepath}"
    await _reindex_singleflight.run(
        key, lambda: _reindex_one(filepath, content, project_id)
    )


def _dispatch_ide_telemetry(payload: IdeTelemetryPayload, project_id: str) -> None:
    """Route a silent IDE lifecycle signal into the existing reactive-index seam.

    Pure non-blocking enqueue: io_coalescer hands the work to the off-loop
    indexing worker, so the event loop never indexes inline. ``content=""``
    mirrors the file-update path — the reactive indexer reads the freshest bytes
    from the RAM-VFS buffer or disk. A rename purges the old node and re-submits
    the new path so the graph migrates instead of orphaning a stale entry. Both
    sides run under the session's real project_id so the partition matches.
    """
    if payload.action == "file_renamed" and payload.old_path:
        io_coalescer.submit_unlink(payload.old_path, project_id=project_id)
        io_coalescer.submit(payload.filepath, "", project_id=project_id)
    else:  # file_saved / file_created
        io_coalescer.submit(payload.filepath, "", project_id=project_id)


@app.websocket("/api/v1/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str) -> None:
    """
    La Puerta Principal de Streaming (WebSockets).
    Ruta estandarizada para coincidir con ws_client.ts.
    """
    # 1. El Manager acepta y registra la conexión.
    # Phase 7.9.A.5.1: pass ephemeral token; connect() validates first message.
    connected = await vfs_manager.connect(client_id, websocket, auth_token=_AUTH_TOKEN)
    if not connected:
        return

    try:
        # 2. El Bucle Infinito de Escucha
        while True:
            # Esperamos a que VS Code envíe un mensaje
            raw_data = await websocket.receive_text()

            # 3. Pasamos el mensaje por nuestro Escudo de Pydantic
            valid_event = await vfs_manager.validate_incoming(raw_data)

            if valid_event is None:
                continue

            # --- ZONA DE ENRUTAMIENTO SEGURO ---
            logger.info(
                "📥 Evento válido de %s: %s", client_id, valid_event.event_type
            )

            if valid_event.event_type == "client_register_session":
                # Multiplexing handshake: alias this session id onto the physical
                # socket (connected under client_id) so send_personal_message routes
                # the session's events here. Re-sent by the client on every
                # reconnect, so it must be idempotent.
                vfs_manager.register_alias(valid_event.data.session_id, client_id)

            elif valid_event.event_type == "client_file_update":
                # Inbound flood guard: shed save storms past the per-client token
                # bucket so a runaway watcher cannot starve the loop. Only this
                # telemetry-class event is gated; interactive events below never
                # are. The 500ms coalescer + reactive single-flight already absorb
                # normal editing, so the bucket only trips on pathological rates.
                if not vfs_manager.allow_inbound(client_id):
                    logger.debug("Inbound rate limit: shed client_file_update from %s", client_id)
                    continue
                # 1. RAM-VFS ingestion — synchronous O(1), does not block the event loop
                task_service.vfs.ingest_dirty_buffers(
                    # api_contracts.DirtyBuffer and vfs_middleware.DirtyBuffer are
                    # structurally-identical DTOs duplicated across the transport/core
                    # boundary (pre-existing); ingestion is duck-typed at runtime.
                    cast(List[VfsDirtyBuffer], [DirtyBuffer(path=valid_event.data.filepath, content=valid_event.data.content)])
                )
                # 2. Critical files bypass debounce; normal files coalesced in 500ms window
                _proj = _session_project_id.get(client_id, "")
                # A live edit invalidates any in-flight consolidation snapshot.
                _abort_dreaming(_proj)
                if is_critical_file(valid_event.data.filepath):
                    asyncio.create_task(
                        _dispatch_indexing_and_ppr(
                            valid_event.data.filepath,
                            valid_event.data.content,
                            project_id=_proj,
                        ),
                        name=f"index_critical:{valid_event.data.filepath}",
                    )
                else:
                    io_coalescer.submit(
                        valid_event.data.filepath, valid_event.data.content, project_id=_proj
                    )

            elif valid_event.event_type == "client_ide_telemetry":
                # Silent file-lifecycle push. Telemetry-class: shed past the same
                # per-client token bucket as client_file_update so a save storm
                # cannot starve the loop; interactive events below are never gated.
                # The accepted frame was already mirrored to the telemetry log by
                # validate_incoming. Dispatch is a non-blocking coalescer enqueue.
                if not vfs_manager.allow_inbound(client_id):
                    logger.debug("Inbound rate limit: shed client_ide_telemetry from %s", client_id)
                    continue
                _tel_proj = _session_project_id.get(client_id, "")
                # A live file lifecycle event invalidates an in-flight snapshot.
                _abort_dreaming(_tel_proj)
                # A README save/create reactively re-warms the orientation digest
                # (debounced downstream, so a save storm collapses to one build).
                if (
                    valid_event.data.action in ("file_saved", "file_created")
                    and os.path.basename(valid_event.data.filepath) == "README.md"
                ):
                    task_service.warm_readme_digest(
                        _tel_proj or None,
                        _session_workspace_root.get(client_id, ""),
                        client_id,
                    )
                _dispatch_ide_telemetry(
                    valid_event.data,
                    _tel_proj,
                )

            elif valid_event.event_type == "client_dreaming_run":
                # Manual Dreaming: explicit, user-owned consolidation. Interactive
                # frame — never rate-limited; runs one pass per project at a time.
                _trigger_dreaming(
                    client_id,
                    valid_event.data.focus_area,
                )

            elif valid_event.event_type == "client_export_memory_snapshot":
                # Fire-and-forget off the receive loop: a large-graph compress must
                # not stall this client's WebSocket. Best-effort — logged, not acked.
                asyncio.create_task(
                    export_memory_snapshot(
                        valid_event.data.project_id,
                        valid_event.data.workspace_root,
                    ),
                    name=f"export_snapshot:{valid_event.data.project_id}",
                )

            elif valid_event.event_type == "client_planner_mode_toggle":
                planner_mode_registry[client_id] = valid_event.data.active
                logger.info(
                    "[Session: %s] Persistence: MANUAL_PLANNING set to %s",
                    client_id,
                    valid_event.data.active,
                )

            elif valid_event.event_type == "client_hitl_response":
                # Two HITL transports: a paused graph (native interrupt) resumes via
                # Command(resume=…); everything else (MCP adapter, post-graph file-write
                # apply loop) still resolves the in-memory approval Event. resume_graph
                # pops the paused entry first, so a duplicate reply is a harmless no-op.
                if task_service.has_paused_graph(client_id):
                    _approval = {
                        "approved": valid_event.data.approved,
                        "comment": valid_event.data.comment,
                    }
                    # Background task — never block the WS receive loop on the resume.
                    _resume_t = asyncio.create_task(
                        task_service.resume_graph(client_id, _approval)
                    )
                    task_service.register_active_task(client_id, _resume_t)
                else:
                    vfs_manager.resolve_human_approval(
                        approval_id=valid_event.data.approval_id,
                        approved=valid_event.data.approved,
                        comment=valid_event.data.comment,
                        modified_content=valid_event.data.modified_content,
                    )
                # Confirm receipt so a response from a hidden/torn-down webview is
                # never silently orphaned.
                await vfs_manager.broadcast_hitl_ack(
                    client_id, valid_event.data.approval_id, True
                )
                logger.info(
                    "✅ HITL response from %s: approved=%s (approval_id=%s)",
                    client_id,
                    valid_event.data.approved,
                    valid_event.data.approval_id,
                )

            elif valid_event.event_type == "client_patch_applied":
                # Phase 7.9.B.18 — host ack for an applyEdit dispatch; unblocks
                # write_pipeline.apply_patch_set's waiter.
                vfs_manager.resolve_patch_ack(
                    valid_event.data.patch_id, valid_event.data.model_dump()
                )
                logger.info(
                    "🩹 Patch ack from %s: patch_id=%s ok=%s",
                    client_id, valid_event.data.patch_id, valid_event.data.ok,
                )

            elif valid_event.event_type == "client_devcontainer_provision_status":
                # Host reports the devcontainer provisioning lifecycle; only a
                # terminal state resolves the trusted-tier bridge's waiter.
                vfs_manager.resolve_devcontainer_provision(
                    valid_event.data.request_id, valid_event.data.state
                )

            elif valid_event.event_type == "client_devcontainer_exec_stream":
                # Incremental stdout/stderr chunk for a running devcontainer command.
                vfs_manager.append_devcontainer_stream(
                    valid_event.data.request_id,
                    valid_event.data.stream,
                    valid_event.data.chunk,
                )

            elif valid_event.event_type == "client_devcontainer_exec_exit":
                # Devcontainer command finished; wake the bridge's exec waiter.
                vfs_manager.resolve_devcontainer_exit(
                    valid_event.data.request_id, valid_event.data.exit_code
                )

            elif valid_event.event_type == "client_concurrency_conflict":
                logger.warning(
                    "⚡ OCC Conflict [Session: %s]: %s (expected v%d, got v%d) — aborting write",
                    client_id,
                    valid_event.data.filepath,
                    valid_event.data.expected_version,
                    valid_event.data.actual_version,
                )

            elif valid_event.event_type == "client_workspace_init":
                _workspace_registry[valid_event.data.project_id] = valid_event.data.workspace_root
                _session_workspace_root[client_id] = valid_event.data.workspace_root
                # Bind this session to its project so reactive saves index into the
                # same partition the agent's RAG consumer reads (not the "" orphan).
                _session_project_id[client_id] = valid_event.data.project_id
                # Bind the per-project GraphRAG store so the semantic index writes
                # into this project's own directory under the application home.
                storage_paths.bind_project(valid_event.data.workspace_root)
                # Persist the id -> workspace-root mapping so the dashboard's
                # active-project selector can name this project after a restart,
                # independent of whether an editor window is currently connected.
                try:
                    await catalog_db.upsert_project(
                        valid_event.data.project_id,
                        valid_event.data.workspace_root,
                    )
                except Exception:  # noqa: BLE001 — registry write must never block session init
                    logger.warning("Project registry upsert failed", exc_info=True)
                # Point the live telemetry sink at this workspace (idempotent).
                configure_telemetry_log(valid_event.data.workspace_root)
                if valid_event.data.workspace_pid is not None:
                    _session_workspace_pid[client_id] = valid_event.data.workspace_pid
                # Warm-start the dependency graph from a committed snapshot before the
                # full crawl, so graph-aware tools work in seconds on a fresh clone.
                # Fail-open: a missing or bad artifact simply defers to the crawl.
                try:
                    await import_memory_snapshot(
                        valid_event.data.project_id,
                        valid_event.data.workspace_root,
                    )
                except Exception:  # noqa: BLE001 — bootstrap must never block session init
                    logger.warning("Memory snapshot bootstrap failed", exc_info=True)
                await lazy_indexer.start(
                    workspace_root=valid_event.data.workspace_root,
                    project_id=valid_event.data.project_id,
                    session_id=client_id,
                )
                # Warm the README orientation digest in the background so a large
                # README is ready before the analyst is first asked about the repo.
                task_service.warm_readme_digest(
                    valid_event.data.project_id,
                    valid_event.data.workspace_root,
                    client_id,
                )
                logger.info(
                    "[Session: %s] Workspace init received: root=%s project=%s",
                    client_id,
                    valid_event.data.workspace_root,
                    valid_event.data.project_id,
                )

            elif valid_event.event_type == "client_clear_conversation":
                # Phase 7.9.B.15 — drop short-term chat memory for this session.
                task_service.clear_conversation(client_id)
                logger.info("[Session: %s] Conversation memory cleared.", client_id)

            elif valid_event.event_type == "client_restore_history":
                # Phase 7.9.B.20 — rehydrate a reopened session's memory for continuity.
                task_service.restore_conversation(
                    client_id, [m.model_dump() for m in valid_event.data.messages]
                )
                # Re-surface a HITL interrupt that was suspended before a server restart:
                # restore the checkpoint + pending writes and re-emit the approval card so
                # the operator can still resume. No-op for a live or non-paused session.
                await task_service.rehydrate_paused_interrupt(client_id)

            elif valid_event.event_type == "client_analyst_query":
                # Phase 7.9.B.13 — Natt analyst pane bridge (live BYOM completion).
                # Phase 7.9.B.17 — run it off the WS receive loop so a slow model
                # never stalls inbound message processing for this session.
                # Phase 7.10.3 (ADR-703) — forward context_paths + cursor + project so the
                # analyst answers with active-file + Codex + RAG context, streamed in batches.
                _q = valid_event.data
                _aq_root = _session_workspace_root.get(client_id, "")
                _aq_proj = next(
                    (pid for pid, root in _workspace_registry.items()
                     if _aq_root and root == _aq_root),
                    None,
                )

                async def _analyst_runner(
                    text: str = _q.text,
                    sid: str = client_id,
                    paths: List[str] = list(_q.context_paths),
                    cursor: Optional[int] = _q.cursor,
                    project_id: Optional[str] = _aq_proj,
                    project_root: str = _aq_root,
                    model_tier: Optional[str] = _q.model_tier,
                ) -> None:
                    # Phase 7.11.3 — register THIS runner with the abort mesh
                    # (plan W1 invariant: current_task() is the spawned runner,
                    # NEVER the WS receive loop). A Stop click for the same
                    # session_id will Task.cancel() us; stream_analyst_reply
                    # catches CancelledError and emits the abort marker.
                    _ar_task = asyncio.current_task()
                    if _ar_task is not None:
                        task_service.register_active_task(sid, _ar_task)
                    await task_service.stream_analyst_reply(
                        sid, text, paths, cursor, project_id, project_root, model_tier
                    )
                    logger.info(
                        "[Session: %s] Analyst query handled (%d chars in, %d path(s))",
                        sid, len(text), len(paths),
                    )

                _at = asyncio.create_task(_analyst_runner(), name=f"analyst:{client_id}")
                _task_submit_tasks.add(_at)
                _at.add_done_callback(_task_submit_tasks.discard)

            elif valid_event.event_type == "client_inline_edit_request":
                # Phase 7.11.1 (ADR-706 §4.5a) — Cmd+K inline edit stream.
                # Backend reads its baseline content from the live RAM-VFS (the
                # extension's last client_file_update); the frontend stays the
                # source of truth for selection ranges, which are LF-space
                # offsets per plan W1.
                _ier = valid_event.data
                _baseline = task_service.vfs.read(_ier.file_path) or _ier.selected_text
                _norm_baseline = _baseline.replace("\r\n", "\n")

                async def _inline_runner(
                    sid: str = client_id,
                    edit_id: str = _ier.edit_id,
                    file_path: str = _ier.file_path,
                    file_content: str = _norm_baseline,
                    range_start: int = _ier.range_start,
                    range_end: int = _ier.range_end,
                    prompt: str = _ier.prompt,
                    language_id: Optional[str] = _ier.language_id,
                ) -> None:
                    await task_service.start_inline_edit(
                        session_id=sid,
                        edit_id=edit_id,
                        file_path=file_path,
                        file_content=file_content,
                        range_start=range_start,
                        range_end=range_end,
                        prompt=prompt,
                        language_id=language_id,
                    )

                _iet = asyncio.create_task(
                    _inline_runner(), name=f"inline_edit:{_ier.edit_id}"
                )
                _task_submit_tasks.add(_iet)
                _iet.add_done_callback(_task_submit_tasks.discard)

            elif valid_event.event_type == "client_inline_edit_cancel":
                # Phase 7.11.1 — cooperative cancel. Sets cancel_event + Task.cancel().
                _did_cancel = task_service.cancel_inline_edit(valid_event.data.edit_id)
                logger.info(
                    "[Session: %s] Inline edit cancel: edit_id=%s found=%s",
                    client_id, valid_event.data.edit_id, _did_cancel,
                )

            elif valid_event.event_type == "client_abort_mesh":
                # Phase 7.11.3 (ADR-706 §4.5b) — priority abort. We resolve
                # session_id → the registered runner asyncio.Task via the
                # TaskService registry and call task.cancel() — NEVER the WS
                # receive task itself (that would terminate the connection;
                # see plan W1). The runner's CancelledError handler in
                # task_service writes the savepoint marker + emits the
                # "Stopped by user" turn + closes the stream.
                # Signal the live terminal first so the foreground process gets a
                # Ctrl-C immediately, then cancel the runner task. Best-effort: a
                # session-less abort just skips this.
                await task_service.interrupt_session(client_id)
                _did_abort = task_service.abort_session(client_id)
                # ACK so the UI never leaves the Stop button frozen: signalled=False
                # tells the client no live task existed (already done / never ran).
                await vfs_manager.broadcast_abort_ack(client_id, _did_abort)
                logger.info(
                    "[Session: %s] Abort mesh: signalled=%s", client_id, _did_abort,
                )

            elif valid_event.event_type == "client_pty_write":
                # Phase 7.19.6 — interactive terminal: feed a line of stdin to the
                # session's live persistent terminal so the user can answer a
                # blocking prompt. Fire-and-forget; a missing session is a no-op.
                _pw = valid_event.data
                _wrote = await task_service.write_session_stdin(
                    client_id, _pw.data.encode("utf-8")
                )
                logger.debug(
                    "[Session: %s] pty stdin: wrote=%s", client_id, _wrote,
                )

            elif valid_event.event_type == "client_retry_tool":
                # Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips: exact-replay
                # retry. The runner is a CHILD task (asyncio.create_task) so
                # that this branch returns immediately and the WS receive loop
                # stays responsive. We deliberately do NOT register the runner
                # into _active_tasks (the abort mesh) — clicking Stop should
                # not cancel a deliberate Retry mid-flight (plan W1 carried).
                _rt = valid_event.data
                async def _retry_runner(
                    sid: str = client_id,
                    tcid: str = _rt.tool_call_id,
                ) -> None:
                    ok = await task_service.retry_tool_call(sid, tcid)
                    logger.info(
                        "[Session: %s] retry_tool_call: ok=%s id=%s",
                        sid, ok, tcid,
                    )
                _retry_task = asyncio.create_task(
                    _retry_runner(), name=f"tool_retry:{_rt.tool_call_id}"
                )
                _task_submit_tasks.add(_retry_task)
                _retry_task.add_done_callback(_task_submit_tasks.discard)

            elif valid_event.event_type == "client_invoke_tracked_bash":
                # Phase 7.11.6 — dev smoke command (palette `/dev/run-bash`).
                # Routes through execute_tracked_tool so the chip pipeline is
                # provably alive end-to-end without needing an agent refactor.
                _ib = valid_event.data
                async def _bash_runner(
                    sid: str = client_id,
                    cmd: str = _ib.command,
                    tmo: float = _ib.timeout_sec,
                    wd: Optional[str] = _ib.working_dir,
                ) -> None:
                    await task_service.execute_tracked_tool(
                        session_id=sid,
                        tool_name="sandbox_bash",
                        args={
                            "command": cmd,
                            "timeout_sec": tmo,
                            "working_dir": wd,
                        },
                        # sandbox_bash mutates state by default — Retry
                        # confirmation toast must fire on the frontend.
                        side_effect_free=False,
                    )
                _bash_task = asyncio.create_task(
                    _bash_runner(), name=f"tracked_bash:{client_id}"
                )
                _task_submit_tasks.add(_bash_task)
                _bash_task.add_done_callback(_task_submit_tasks.discard)

            elif valid_event.event_type == "client_branch_from_checkpoint":
                # Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel Debugging.
                # Fork a session from a historical checkpoint. The runner is a
                # child task (same shape as retry_tool / tracked_bash above):
                # NOT registered into _active_tasks (the abort mesh) because a
                # branch op is a deliberate user action and clicking Stop on the
                # parent session should not cancel it (plan W1 carried).
                _bc = valid_event.data
                _new_session_id = uuid.uuid4().hex
                async def _branch_runner(
                    parent_sid: str = _bc.parent_session_id,
                    from_cid: str = _bc.from_checkpoint_id,
                    new_sid: str = _new_session_id,
                ) -> None:
                    ok = await task_service.branch_session(
                        parent_session_id=parent_sid,
                        from_checkpoint_id=from_cid,
                        new_session_id=new_sid,
                    )
                    logger.info(
                        "[Session: %s] branch_session: ok=%s from=%s -> new=%s",
                        parent_sid, ok, from_cid, new_sid,
                    )
                _branch_task = asyncio.create_task(
                    _branch_runner(), name=f"branch_session:{_new_session_id}"
                )
                _task_submit_tasks.add(_branch_task)
                _branch_task.add_done_callback(_task_submit_tasks.discard)

            elif valid_event.event_type == "client_file_delete":
                # Purge the same partition the session's saves wrote into, so a
                # delete migrates the live graph rather than an orphan keyed on a
                # client-supplied project_id.
                io_coalescer.submit_unlink(
                    valid_event.data.filepath,
                    _session_project_id.get(client_id, ""),
                )

            elif valid_event.event_type == "client_master_toggle":
                from core.config.profile import (
                    load_from_workspace,
                    save_to_workspace,
                    WorkspaceRootMissingError,
                )
                try:
                    ws_root = _session_workspace_root.get(client_id)
                    cfg = load_from_workspace(ws_root)
                    cfg = cfg.model_copy(update={"master_enabled": valid_event.data.enabled})
                    save_to_workspace(ws_root, cfg)
                except WorkspaceRootMissingError as exc:
                    logger.warning("master_toggle ignored: %s", exc)

            elif valid_event.event_type == "client_profile_change":
                from core.config.profile import (
                    load_from_workspace,
                    save_to_workspace,
                    WorkspaceRootMissingError,
                )
                try:
                    ws_root = _session_workspace_root.get(client_id)
                    cfg = load_from_workspace(ws_root)
                    cfg = cfg.model_copy(update={"profile": valid_event.data.profile})
                    save_to_workspace(ws_root, cfg)
                except WorkspaceRootMissingError as exc:
                    logger.warning("profile_change ignored: %s", exc)

    except WebSocketDisconnect:
        # 4. Limpieza O(1) para evitar Fugas de Memoria
        logger.warning(f"⚠️ Conexión perdida abruptamente con {client_id}")
        vfs_manager.disconnect(client_id, websocket)
        # Evict per-session maps so a reconnect storm cannot grow them unboundedly
        # (one entry per historical connection would otherwise leak for process life).
        _disc_proj = _session_project_id.pop(client_id, None)
        _session_workspace_root.pop(client_id, None)
        # Cancel + evict any consolidation bound to this session's project so a
        # reconnect storm cannot leak tasks or epoch counters for process life.
        if _disc_proj:
            _disc_task = _dreaming_tasks.pop(_disc_proj, None)
            if _disc_task is not None and not _disc_task.done():
                _disc_task.cancel()
            _dreaming_epoch.pop(_disc_proj, None)
        if client_id in _session_workspace_pid:
            _pid = _session_workspace_pid.pop(client_id)
            asyncio.create_task(lifecycle_manager.shutdown_workspace(_pid))


# Phase 7.9.A.5.1 — explicit entry point so the extension can spawn with dynamic port.
# The extension always passes --host 127.0.0.1 --port {port} as spawn args; _API_PORT
# is the fallback for manual `python main.py` runs during development.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=_API_PORT, reload=False)
