import asyncio
from typing import Dict, List
from fastapi import WebSocket, WebSocketDisconnect
import json

# Clase para gestionar múltiples conexiones WebSocket de forma segura.
# Aplicamos el patrón Singleton de facto al instanciarlo en el core.
class ConnectionManager:
    """
    Gestiona el ciclo de vida de las conexiones WebSocket.
    Optimización: O(1) para búsquedas de conexión por task_id.
    """
    def __init__(self):
        # Almacenamos conexiones activas: {task_id: WebSocket}
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, task_id: str, websocket: WebSocket):
        """Acepta la conexión y la registra en el pool."""
        await websocket.accept()
        self.active_connections[task_id] = websocket
        print(f"DEBUG: Conexión establecida para tarea {task_id}")

    def disconnect(self, task_id: str):
        """Elimina la conexión del pool para evitar memory leaks."""
        if task_id in self.active_connections:
            del self.active_connections[task_id]
            print(f"DEBUG: Conexión cerrada para tarea {task_id}")

    async def send_personal_message(self, message: dict, task_id: str):
        """Envía un mensaje JSON a un cliente específico."""
        if task_id in self.active_connections:
            websocket = self.active_connections[task_id]
            # Usamos json.dumps para asegurar que el contrato de I/O se cumpla
            await websocket.send_text(json.dumps(message))

    async def broadcast_telemetry(self, telemetry_data: dict):
        """
        Envía datos de telemetría a todos los clientes (opcional).
        Complejidad: O(n) donde n es el número de conexiones activas.
        """
        for task_id, connection in self.active_connections.items():
            try:
                await connection.send_json(telemetry_data)
            except Exception:
                # Si una conexión falla, la ignoramos para no bloquear el resto
                continue

# Instancia global para ser importada en main.py
manager = ConnectionManager()