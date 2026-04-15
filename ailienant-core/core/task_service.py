from pydantic import BaseModel
from typing import List, Dict, Any
from .vfs_middleware import VFSMiddleware, DirtyBuffer
import logging

logger = logging.getLogger(__name__)

# Replicamos el contrato del frontend (api_client.ts)
class TaskPayload(BaseModel):
    task_prompt: str
    dirty_buffers: List[DirtyBuffer]

class TaskService:
    """
    Capa de orquestación intermedia. 
    Aísla la lógica de LangGraph y VFS de la capa de transporte HTTP.
    """
    def __init__(self):
        # Inyección de dependencias (Singleton)
        self.vfs = VFSMiddleware()

    async def process_task(self, session_id: str, payload: TaskPayload) -> Dict[str, Any]:
        """
        Asimila la entropía y dispara el motor cognitivo.
        """
        logger.info(f"[Session: {session_id}] Iniciando misión cognitiva. Interceptando {len(payload.dirty_buffers)} buffers.")
        
        # 1. Asimilación de Entropía O(1)
        # El VFS se actualiza en RAM antes de que LangGraph despierte.
        self.vfs.ingest_dirty_buffers(payload.dirty_buffers)

        # 2. Despertar a LangGraph (Placeholder para la siguiente fase)
        # Aquí es donde llamaremos al 'graph.invoke()' más adelante
        logger.info(f"[Session: {session_id}] VFS Sincronizado. Prompt: {payload.task_prompt}")
        
        return {
            "status": "success", 
            "message": "Entropía asimilada en VFS. LangGraph listo para iterar.",
            "session_id": session_id
        }