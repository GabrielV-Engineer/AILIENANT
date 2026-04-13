# core/main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from state import AilienantGraphState, ContextMeter, AgentMemorySnapshot
# --- NUEVA IMPORTACIÓN ---
from websocket_manager import manager  # Importamos la instancia global del gestor
import json
import asyncio

app = FastAPI(
    title="Ailienant Core Engine",
    description="Motor de orquestación de agentes híbridos con soporte para LangGraph y GraphRAG"
)

@app.get("/")
async def root():
    return {"status": "online", "message": "Ialienant Core is running"}

@app.websocket("/ws/v1/stream/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """
    Canal de streaming optimizado. 
    Ahora gestionado por ConnectionManager para evitar fugas de memoria.
    """
    # 1. Registrar y aceptar la conexión a través del manager
    await manager.connect(task_id, websocket)
    
    try:
        # 2. Simulación de flujo (Mantenemos tu lógica de Mock pero usando el manager)
        mock_telemetry = {
            "type": "TELEMETRY_UPDATE",
            "payload": {
                "css_score": 85.0,
                "routing": "LOCAL_SMALL",
                "latency_ms": 45
            }
        }
        # Enviamos mensaje específico a este task_id
        await manager.send_personal_message(mock_telemetry, task_id)
        
        await asyncio.sleep(1) # Simulación de latencia de red/pensamiento
        
        token_chunk = {
            "type": "TOKEN_CHUNK",
            "payload": { "agent": "PlannerAgent", "text": "IAlienant: Analizando arquitectura..." }
        }
        await manager.send_personal_message(token_chunk, task_id)

        # 3. Bucle de escucha (Keep-alive)
        while True:
            # Esperamos datos del cliente (ej. una aprobación de paso del WBS)
            data = await websocket.receive_text()
            # Aquí procesaríamos el input si fuera necesario
            print(f"Mensaje recibido de {task_id}: {data}")

    except WebSocketDisconnect:
        # 4. Manejo de desconexión limpia
        print(f"INFO: El cliente {task_id} se ha desconectado.")
    except Exception as e:
        print(f"ERROR: Error inesperado en socket {task_id}: {e}")
    finally:
        # 5. SIEMPRE liberar el recurso en el pool del manager
        manager.disconnect(task_id)