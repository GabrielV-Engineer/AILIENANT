from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment
import logging

logger = logging.getLogger(__name__)


# Replicamos el contrato del frontend (api_client.ts)
class TaskPayload(BaseModel):
    task_prompt: str
    dirty_buffers: List[DirtyBuffer]
    project_id: Optional[str] = None
    explicit_mentions: List[str] = Field(default_factory=list)
    attachments: List[ManualAttachment] = Field(default_factory=list)
    document_version_id: Optional[str] = None  # OCC: version at submission (Phase 1.5)


class TaskService:
    """
    Capa de orquestación intermedia.
    Aísla la lógica de LangGraph y VFS de la capa de transporte HTTP.
    """

    def __init__(self):
        # Inyección de dependencias (Singleton)
        self.vfs = VFSMiddleware()

    async def process_task(
        self, session_id: str, payload: TaskPayload
    ) -> Dict[str, Any]:
        """
        Asimila la entropía y dispara el motor cognitivo.
        """
        logger.info(
            f"[Session: {session_id}][Project: {payload.project_id}] "
            f"Iniciando misión cognitiva. Interceptando {len(payload.dirty_buffers)} buffers, "
            f"{len(payload.explicit_mentions)} menciones explícitas, {len(payload.attachments)} adjuntos."
        )

        # 1. Asimilación de Entropía O(1)
        # El VFS se actualiza en RAM antes de que LangGraph despierte.
        self.vfs.ingest_dirty_buffers(payload.dirty_buffers)

        # 2. Despertar a LangGraph (Phase 2 wire-up)
        # result = await run_planner_node(graph_state)  ← async contract ready
        logger.info(
            f"[Session: {session_id}] VFS Sincronizado. Prompt: {payload.task_prompt}"
        )

        return {
            "status": "success",
            "message": "Entropía asimilada en VFS. LangGraph listo para iterar.",
            "session_id": session_id,
        }
