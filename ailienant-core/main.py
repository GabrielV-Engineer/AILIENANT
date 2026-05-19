import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List

import httpx

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
from pydantic import BaseModel
from shared.config import LITELLM_PROXY_API_KEY, LITELLM_PROXY_BASE_URL

# --- IMPORTACIONES FASE 2 (Persistencia y Mantenimiento) ---
from brain.checkpoint import checkpoint_manager

# --- IMPORTACIONES FASE 3.4.5 (MCTS Mirror) ---
from api.mcts_mirror import MergeReport, apply_merge, get_virtual_file

# --- IMPORTACIONES FASE 3.4.7 (Silent Telemetry + Rule Distillation) ---
from agents.analyst import distill_rejection_to_rule
from core.rules import rule_manager

# --- IMPORTACIONES FASE 3.4.8 (Hybrid Cognitive Architecture) ---
from core.token_ledger import token_ledger

# --- IMPORTACIONES FASE 3.5 (Memory Janitor) ---
from core.janitor import JanitorReport, run_janitor

# --- IMPORTACIONES FASE 2.3 (Process Pool e Indexing) ---
from core.compute_pool import compute_pool
from brain.memory import _worker_init, index_file_sync, calculate_ppr_sync

# --- IMPORTACIONES FASE 2.5 (Lazy Indexer) ---
from core.indexer import lazy_indexer

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
    await resolve_default_adapter()          # Phase 6.1.4 — bind sandbox tier
    await catalog_db.init_db()
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
        # Delegación a la capa de servicio (O(1) Memory Ingestion)
        result = await task_service.process_task(session_id=x_task_id, payload=payload)
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Fallo crítico en el motor cognitivo: {str(e)}")
        raise HTTPException(status_code=500, detail="Colapso interno en el orquestador")


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
