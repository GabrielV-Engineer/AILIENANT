# alienant-core/core/websocket_manager.py

from fastapi import WebSocket
from typing import Dict, Optional
from pydantic import ValidationError, TypeAdapter
import logging
from ws_contracts import WebSocketMessage

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VFS_Manager")

# =====================================================================
# ADAPTADOR DE TIPOS (Pydantic V2)
# =====================================================================
# Compilamos el esquema del Tagged Union una sola vez al inicio.
# Esto reduce la latencia de validación en cada mensaje del WebSocket.
ws_adapter = TypeAdapter(WebSocketMessage)


class ConnectionManager:
    """
    Patrón Singleton para gestionar conexiones persistentes del VFS.
    Aísla la complejidad de la red de la lógica del grafo.
    """

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(
            f"🟢 IDE Conectado: {client_id}. Total activos: {len(self.active_connections)}"
        )

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"🔴 IDE Desconectado: {client_id}")

    async def send_personal_message(self, client_id: str, event: WebSocketMessage):
        if client_id in self.active_connections:
            # TypeAdapter también es excelente para serializar hacia afuera
            payload = ws_adapter.dump_json(event).decode("utf-8")
            await self.active_connections[client_id].send_text(payload)

    async def validate_incoming(
        self, raw_json_string: str
    ) -> Optional[WebSocketMessage]:
        """
        EL ESCUDO: Todo mensaje entrante pasa por el TypeAdapter.
        Validación O(1) de Uniones Discriminadas.
        """
        try:
            # Usamos el adaptador en lugar del método que no existía
            valid_event = ws_adapter.validate_json(raw_json_string)
            return valid_event
        except ValidationError as e:
            logger.error(
                f"⚠️ Inyección rechazada en la frontera: Payload malformado. Detalles: {e}"
            )
            return None


# Instancia Singleton Global
vfs_manager = ConnectionManager()
