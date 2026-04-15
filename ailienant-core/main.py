# alienant-core/main.py

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
import logging
import uvicorn
import uuid

# Importamos nuestro motor de red y contratos
from api.websocket_manager import vfs_manager
from api.ws_contracts import WebSocketMessage
from api.api_contracts import TaskSubmitRequest, TaskSubmitResponse

# Configuración inicial de la aplicación
app = FastAPI(
    title="AILIENANT VFS Server", 
    description="Backend bicefálico con VFS Middleware O(1)",
    version="0.1.0"
)
logger = logging.getLogger("AILIENANT_API")

@app.get("/")
async def health_check():
    """Endpoint HTTP tradicional para verificar que el servidor está vivo."""
    return {"status": "online", "phase": "0.1", "system": "VFS Middleware Active"}

@app.post("/task/submit", response_model=TaskSubmitResponse)
async def submit_task(payload: TaskSubmitRequest):
    """
    Endpoint de inicialización de misiones.
    Aquí es donde el humano (Capa 8) le da la orden al Orquestador.
    """
    try:
        # Generamos un ID de misión trazable
        task_id = f"mission_{uuid.uuid4().hex[:8]}"
        
        logger.info(f"🚀 Misión [{task_id}] recibida.")
        logger.info(f"📂 Archivo activo: {payload.ide_context.active_file}")
        logger.info(f"📝 Buffers sucios detectados: {len(payload.ide_context.dirty_buffers)}")
        
        # AQUÍ (En Fase 1) inicializaremos el thread en SQLite usando engine.py 
        # y mapearemos los dirty_buffers al read_files_state de LangGraph.
        
        return TaskSubmitResponse(
            task_id=task_id,
            status="accepted",
            message="Misión aceptada. Contexto VFS sincronizado."
        )
    except Exception as e:
        logger.error(f"🔴 Error procesando la misión: {e}")
        raise HTTPException(status_code=500, detail="Error interno del Orquestador.")

@app.websocket("/vfs/ws/{client_id}")
async def vfs_websocket_endpoint(websocket: WebSocket, client_id: str):
    """
    La Puerta Principal. Aquí se conectará la extensión de VS Code.
    """
    # 1. El Manager acepta y registra la conexión
    await vfs_manager.connect(client_id, websocket)
    
    try:
        # 2. El Bucle Infinito de Escucha
        while True:
            # Esperamos a que VS Code envíe un mensaje
            raw_data = await websocket.receive_text()
            
            # 3. Pasamos el mensaje por nuestro Escudo de Pydantic V2
            valid_event = await vfs_manager.validate_incoming(raw_data)
            
            if valid_event is None:
                # Si es inválido, el manager ya logueó el error. Ignoramos y seguimos escuchando.
                continue
                
            # --- ZONA DE ENRUTAMIENTO SEGURO ---
            # Si llegamos aquí, sabemos con 100% de certeza que valid_event cumple los contratos.
            logger.info(f"📥 Evento válido procesado de {client_id}: {valid_event.event_type}")
            
            # (En la Fase 1, aquí inyectaremos este evento al estado de LangGraph)
            
    except WebSocketDisconnect:
        # 4. Limpieza O(1) si el IDE se desconecta bruscamente
        logger.warning(f"⚠️ Conexión perdida abruptamente con {client_id}")
        vfs_manager.disconnect(client_id)

if __name__ == "__main__":
    # Permite ejecutar el archivo directamente con python main.py para desarrollo
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)