import asyncio
import os
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, cast
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment, AIlienantGraphState
from brain.engine import alienant_app
from api.websocket_manager import vfs_manager
from langchain_core.runnables import RunnableConfig
import logging

logger = logging.getLogger(__name__)

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: set = set()


# Replicamos el contrato del frontend (api_client.ts)
class TaskPayload(BaseModel):
    task_prompt: str
    dirty_buffers: List[DirtyBuffer]
    project_id: Optional[str] = None
    explicit_mentions: List[str] = Field(default_factory=list)
    attachments: List[ManualAttachment] = Field(default_factory=list)
    document_version_id: Optional[str] = None  # OCC: version at submission (Phase 1.5)
    planner_mode_active: bool = False  # Phase 2.19: Planner-Mode toggle forwarded from WS registry
    workspace_root: Optional[str] = None  # Passed from _workspace_registry at HTTP layer


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
            "[Session: %s][Project: %s] Initiating graph invocation. "
            "buffers=%d mentions=%d attachments=%d planner_mode=%s",
            session_id, payload.project_id, len(payload.dirty_buffers),
            len(payload.explicit_mentions), len(payload.attachments),
            payload.planner_mode_active,
        )

        # 1. Asimilación de Entropía O(1)
        self.vfs.ingest_dirty_buffers(payload.dirty_buffers)

        # 2. Construcción del estado inicial para LangGraph
        initial_state: dict = {
            "task_id": session_id,
            "user_input": payload.task_prompt,
            "project_id": payload.project_id,
            "explicit_mentions": payload.explicit_mentions,
            "attachments": payload.attachments,
            "messages": [],
            "tci": 0.0,
            "css": 100.0,
            "is_manual_override": False,
            "planner_mode_active": payload.planner_mode_active,
            "workspace_root": payload.workspace_root or "",
            "hitl_pending": False,
            "hitl_response": None,
            "shared_understanding_reached": False,
            "target_role": None,
            "current_step_id": None,
            "mission_spec": None,
            "parallel_tasks": [],
            "read_files_state": {},
            "vfs_buffer": {},
            "has_images": any(a.type == "image" for a in payload.attachments),
            "routing_warning": None,
            "hardware_profile": None,
            "provider": "CLOUD",
            "generated_code": {},
            "errors": [],
            "retry_count": 0,
            "security_flags": [],
            "terminal_output": "",
            "session_delta": "",
            "is_indexing_complete": True,
            "guardrail_failed": False,
            "validation_feedback": None,
            "immutable_wbs": None,
            "pending_patches": {},
            "current_cost_usd": 0.0,
            "max_budget_usd": float(os.getenv("AILIENANT_MAX_BUDGET_USD", "inf")),
        }
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}

        # 3. Ejecución del grafo con streaming de actualizaciones
        final_output: dict = {}
        async for update in alienant_app.astream(
            cast(AIlienantGraphState, initial_state), config=config, stream_mode="updates"
        ):
            for node_name, node_output in update.items():
                task = asyncio.create_task(
                    vfs_manager.broadcast_token(
                        session_id,
                        f"[{node_name}] completed",
                        step_id=(
                            node_output.get("current_step_id")
                            if isinstance(node_output, dict) else None
                        ),
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            final_output.update(update)

        return {
            "status": "success",
            "message": "Graph execution completed.",
            "session_id": session_id,
            "output": final_output,
        }
