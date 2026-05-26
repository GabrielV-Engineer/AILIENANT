import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional, cast

import httpx

# Phase 7.9.A.5.1 — ephemeral auth token + dynamic port injected by the extension.
# When AILIENANT_AUTH_TOKEN is absent (manual backend start), auth middleware is bypassed.
_AUTH_TOKEN: Optional[str] = os.environ.get("AILIENANT_AUTH_TOKEN") or None
_API_PORT: int = int(os.environ.get("AILIENANT_API_PORT", "8000"))

# --- IMPORTACIONES FASE 0 (Transporte y WebSockets) ---
from api.api_contracts import ModelInfo, ModelsAvailableResponse
from api.websocket_manager import (
    vfs_manager,
    register_session_cleanup_hook as _register_session_cleanup_hook,
)
from core.lifecycle_manager import lifecycle_manager

# --- IMPORTACIONES FASE 1.2 (Servicio Cognitivo y VFS) ---
from core import db as catalog_db
from core.config_generator import discover_models
from core.db_maintenance import WALCheckpointer
from core.sandbox import resolve_default_adapter
from core.task_service import TaskPayload, TaskService
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
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
from shared.logging_filters import SecretsScrubberFilter  # Phase 6.7 — DLP scrubber
from langchain_core.runnables import RunnableConfig

# --- IMPORTACIONES FASE 3.4.5 (MCTS Mirror) ---
from api.mcts_mirror import MergeReport, apply_merge, get_virtual_file

# --- IMPORTACIONES FASE 3.4.7 (Silent Telemetry + Rule Distillation) ---
from agents.analyst import distill_rejection_to_rule
from core.rules import rule_manager

# --- IMPORTACIONES FASE 3.4.8 (Hybrid Cognitive Architecture) ---
from core.token_ledger import token_ledger
from core.telemetry import recent_oom_events, recent_routing_decisions

# --- IMPORTACIONES FASE 3.5 (Memory Janitor) ---
from core.janitor import JanitorReport, run_janitor

# --- IMPORTACIONES FASE 2.3 (Process Pool e Indexing) ---
from core.compute_pool import compute_pool
from brain.memory import _worker_init, index_file_sync, calculate_ppr_sync

# --- IMPORTACIONES FASE 2.5 (Lazy Indexer) ---
from core.indexer import lazy_indexer

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

# --- IMPORTACIONES FASE 7.9.A.7 (Command-menu backends: agents/mcp/skills) ---
from api.agent_roles import router as agents_router
from api.mcp_servers import router as mcp_router
from api.skills import router as skills_router

# --- IMPORTACIONES FASE 2.6 (I/O Coalescer) ---
from core.io_coalescer import io_coalescer, is_critical_file, _UNLINK_SENTINEL
from shared.contracts import (
    IndexingRequest, IndexingResult, detect_language,
    PPRRequest, PPRResult,
)
from api.api_contracts import DirtyBuffer

# Configuración centralizada de observabilidad
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AILIENANT_API")


# =====================================================================
# LIFESPAN — Startup & Graceful Shutdown
# =====================================================================

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
    await catalog_db.init_db()
    await init_dlq_table()                   # Phase 6.4 — dead_letter_tasks table
    await init_audit_table()                 # Phase 6.6 — hitl_audit_log ledger
    checkpoint_manager.initialize()          # WAL pragmas applied once here
    compute_pool.initialize(initializer=_worker_init)
    io_coalescer.register_dispatch(_dispatch_indexing_and_ppr)
    io_coalescer.register_mass_handler(_handle_mass_change)
    _wal = WALCheckpointer(checkpoint_manager)
    _wal.start()
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
from fastapi import Request
from fastapi.responses import JSONResponse as _JSONResponse

@app.middleware("http")
async def _require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
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


# Dashboard SPA — Phase 7.6 (served at /dashboard)
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

# Phase 7.9.A.7 — Command-menu backends (agent role overrides, MCP registry, skills)
app.include_router(agents_router)
app.include_router(mcp_router)
app.include_router(skills_router)

# Instanciamos nuestra capa de servicio (Inyección de Dependencias)
task_service = TaskService()

# Phase 7.11.6 — wire TaskService.cleanup_session into the WS disconnect path
# so the Rich Tool Chips registry is purged when a client drops. The hook bus
# lives in api.websocket_manager to avoid the circular import that bites if
# the manager imports core.task_service eagerly.
_register_session_cleanup_hook(task_service.cleanup_session)

# Session-scoped planner mode registry — read by TaskService when LangGraph is wired (Phase 2)
planner_mode_registry: Dict[str, bool] = {}

# PPR debounce registry — one asyncio.Task per project_id, cancelled and replaced on each file save
_ppr_tasks: Dict[str, asyncio.Task[None]] = {}
_PPR_DEBOUNCE_S: float = 2.0

# Fire-and-forget task runners (Phase 7.9.B.17). Strong refs prevent the event
# loop from GC-ing an in-flight submit before the agent pipeline finishes.
_task_submit_tasks: set = set()

# Workspace registry — populated on client_workspace_init; used by mass change handler
_workspace_registry: Dict[str, str] = {}  # project_id → workspace_root
_session_workspace_root: Dict[str, str] = {}  # client_id → workspace_root (Phase 3.4.1)
_session_workspace_pid: Dict[str, int] = {}  # client_id → workspace_pid (Phase 4.4)


# =====================================================================
# ENDPOINTS
# =====================================================================

@app.get("/")
async def health_check():
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
):
    """Dispatch a cognitive mission and return immediately (202 Accepted).

    The agent pipeline (planner + coder, or the streaming chat completion) can run
    for far longer than the client's HTTP timeout, so the request must NOT block on
    it. We schedule process_task in the background and stream every result over the
    WebSocket; the HTTP response only acknowledges receipt. Errors inside the runner
    are surfaced over the WS (actionable token + stream_end) so the UI never hangs.
    """

    async def _runner() -> None:
        # Phase 7.11.3 (ADR-706 §4.5b) — register THIS runner task with the
        # abort mesh BEFORE the first await. asyncio.current_task() here
        # returns the runner Task (NOT the WS receive loop's task — that's
        # the disaster case the W1 invariant in the plan guards against).
        # cancel() will propagate CancelledError into the generation coroutine
        # below, where task_service catches it and emits the savepoint marker.
        _runner_task = asyncio.current_task()
        if _runner_task is not None:
            task_service.register_active_task(x_task_id, _runner_task)
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
    _task_submit_tasks.add(_t)
    _t.add_done_callback(_task_submit_tasks.discard)

    return {"status": "accepted", "session_id": x_task_id}


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
    checkpoint_manager.recover(episode.thread_id)  # seed L1 from the L2 snapshot
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
async def list_pending_dlqs(task_id: Optional[str] = None) -> Dict[str, object]:
    """Phase 6.9 — report unresolved DLQ episodes for the sidebar Resume affordance.

    Backend surface for the extension's "Resume Task" item. Optionally scoped to
    a ``task_id``; newest episodes first. Read-only — no state mutation.
    """
    pending = await get_pending_dlqs(task_id)
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
async def http_telemetry_routing(limit: int = 50, offset: int = 0) -> Dict[str, object]:
    """Phase 7.9.B.6 — read recent routing decisions for the dashboard.

    Pagination is clamped server-side (S4+S6) and ``reason`` is secret-masked
    (S1) inside the helper, so this read path is safe to expose read-only.
    """
    return {"decisions": recent_routing_decisions(limit=limit, offset=offset)}


@app.get("/api/v1/telemetry/oom")
async def http_telemetry_oom(limit: int = 50, offset: int = 0) -> Dict[str, object]:
    """Phase 7.9.B.6 — read recent OOM rescue-swap events for the dashboard."""
    return {"events": recent_oom_events(limit=limit, offset=offset)}


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
    """Debounced PPR worker: waits 2s after last file save, then dispatches to process pool."""
    await asyncio.sleep(_PPR_DEBOUNCE_S)
    try:
        edges_raw = await catalog_db.get_all_edges(project_id)
        if not edges_raw:
            return
        req = PPRRequest(edges=tuple((s, t) for s, t in edges_raw))
        result: PPRResult = await compute_pool.run(calculate_ppr_sync, req)
        if result.success and result.scores:
            await catalog_db.upsert_ppr_scores(result.scores, project_id)
            logger.info(
                "PPR computed for project '%s': %d files scored", project_id, len(result.scores)
            )
        elif not result.success:
            logger.warning("PPR failed for project '%s': %s", project_id, result.error)
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


async def _dispatch_indexing_and_ppr(filepath: str, content: str, project_id: str) -> None:
    """Index a file in the process pool, persist its import edges, then debounce PPR."""
    if content == _UNLINK_SENTINEL:
        await catalog_db.purge_file_nodes(filepath, project_id)
        logger.info("Ghost purge: removed DB records for deleted file %s", filepath)
        return
    lang = detect_language(filepath)
    if not lang:
        return  # unsupported file type — no-op
    req = IndexingRequest(file_path=filepath, content=content, language_id=lang)
    try:
        result: IndexingResult = await compute_pool.run(index_file_sync, req)
        if result.success:
            logger.debug("Indexed %s: %d symbols", result.file_path, result.symbol_count)
            if result.imports:
                await catalog_db.upsert_dependencies(result.file_path, result.imports, project_id)
                _schedule_ppr(project_id)
        else:
            logger.warning("Indexing failed for %s: %s", result.file_path, result.error)
    except Exception as exc:
        logger.error("Compute pool dispatch error for %s: %s", filepath, exc)


@app.websocket("/api/v1/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
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

            if valid_event.event_type == "client_file_update":
                # 1. RAM-VFS ingestion — synchronous O(1), does not block the event loop
                task_service.vfs.ingest_dirty_buffers(
                    [DirtyBuffer(path=valid_event.data.filepath, content=valid_event.data.content)]
                )
                # 2. Critical files bypass debounce; normal files coalesced in 500ms window
                if is_critical_file(valid_event.data.filepath):
                    asyncio.create_task(
                        _dispatch_indexing_and_ppr(
                            valid_event.data.filepath,
                            valid_event.data.content,
                            project_id="",
                        ),
                        name=f"index_critical:{valid_event.data.filepath}",
                    )
                else:
                    io_coalescer.submit(
                        valid_event.data.filepath, valid_event.data.content, project_id=""
                    )

            elif valid_event.event_type == "client_planner_mode_toggle":
                planner_mode_registry[client_id] = valid_event.data.active
                logger.info(
                    "[Session: %s] Persistence: MANUAL_PLANNING set to %s",
                    client_id,
                    valid_event.data.active,
                )

            elif valid_event.event_type == "client_hitl_response":
                vfs_manager.resolve_human_approval(
                    approval_id=valid_event.data.approval_id,
                    approved=valid_event.data.approved,
                    comment=valid_event.data.comment,
                    modified_content=valid_event.data.modified_content,
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
                if valid_event.data.workspace_pid is not None:
                    _session_workspace_pid[client_id] = valid_event.data.workspace_pid
                await lazy_indexer.start(
                    workspace_root=valid_event.data.workspace_root,
                    project_id=valid_event.data.project_id,
                    session_id=client_id,
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
                        sid, text, paths, cursor, project_id, project_root
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
                _did_abort = task_service.abort_session(client_id)
                logger.info(
                    "[Session: %s] Abort mesh: signalled=%s", client_id, _did_abort,
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

            elif valid_event.event_type == "client_file_delete":
                io_coalescer.submit_unlink(
                    valid_event.data.filepath, valid_event.data.project_id
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
        vfs_manager.disconnect(client_id)
        if client_id in _session_workspace_pid:
            _pid = _session_workspace_pid.pop(client_id)
            asyncio.create_task(lifecycle_manager.shutdown_workspace(_pid))


# Phase 7.9.A.5.1 — explicit entry point so the extension can spawn with dynamic port.
# The extension always passes --host 127.0.0.1 --port {port} as spawn args; _API_PORT
# is the fallback for manual `python main.py` runs during development.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=_API_PORT, reload=False)
