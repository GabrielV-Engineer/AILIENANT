import logging

# --- IMPORTACIONES FASE 0 (Transporte y WebSockets) ---
from api.websocket_manager import vfs_manager

# --- IMPORTACIONES FASE 1.2 (Servicio Cognitivo y VFS) ---
from core.task_service import TaskPayload, TaskService
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Configuración centralizada de observabilidad
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AILIENANT_API")

app = FastAPI(
    title="AILIENANT API Gateway",
    description="Backend bicefálico con VFS Middleware O(1)",
    version="1.0.0",
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


@app.get("/")
async def health_check():
    """Endpoint HTTP tradicional para verificar que el servidor está vivo."""
    return {"status": "online", "phase": "1.2", "system": "VFS Middleware Active"}


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
                f"📥 Evento válido procesado de {client_id}: {valid_event.event_type}"
            )
            # (En la Fase 1.3, conectaremos LangGraph al WebSocketManager)

    except WebSocketDisconnect:
        # 4. Limpieza O(1) para evitar Fugas de Memoria
        logger.warning(f"⚠️ Conexión perdida abruptamente con {client_id}")
        vfs_manager.disconnect(client_id)
