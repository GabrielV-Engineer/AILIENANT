import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional, cast

import httpx
import os

# --- IMPORTACIONES FASE 0 (Transporte y WebSockets) ---
from api.api_contracts import ModelInfo, ModelsAvailableResponse
from api.websocket_manager import vfs_manager
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

# SecOps: CORS es crítico para el Webview (vscode-webview://)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción limitaremos al URI estricto del Webview
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# Session-scoped planner mode registry — read by TaskService when LangGraph is wired (Phase 2)
planner_mode_registry: Dict[str, bool] = {}

# PPR debounce registry — one asyncio.Task per project_id, cancelled and replaced on each file save
_ppr_tasks: Dict[str, asyncio.Task[None]] = {}
_PPR_DEBOUNCE_S: float = 2.0

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


@app.post("/api/v1/task/submit")
async def submit_task(
    payload: TaskPayload,
    x_task_id: str = Header(
        ..., alias="X-Task-ID"
    ),  # Trazabilidad desde el frontend TS
):
    """
    Endpoint puro de enrutamiento HTTP. Valida I/O y delega la asimilación.
    """
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

        result = await task_service.process_task(
            session_id=x_task_id, payload=payload, execution_mode=resolved_mode
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Fallo crítico en el motor cognitivo: {str(e)}")
        raise HTTPException(status_code=500, detail="Colapso interno en el orquestador")


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
    # 1. El Manager acepta y registra la conexión
    await vfs_manager.connect(client_id, websocket)

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
                )
                logger.info(
                    "✅ HITL response from %s: approved=%s (approval_id=%s)",
                    client_id,
                    valid_event.data.approved,
                    valid_event.data.approval_id,
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
