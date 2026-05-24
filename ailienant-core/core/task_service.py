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


def _summarize_result(state: Dict[str, Any]) -> str:
    """Phase 7.9.B.12 — synthesize a user-facing answer from the merged graph state.

    Defensive against partially-stubbed agents: reports the mission outcome, any
    changed files, validation issues and errors, falling back to a generic line
    when no meaningful output was produced.
    """
    parts: List[str] = []

    mission = state.get("mission_spec")
    outcome = getattr(mission, "outcome", None) if mission is not None else None
    if outcome:
        parts.append(str(outcome))

    changed: set = set()
    pending = state.get("pending_patches")
    generated = state.get("generated_code")
    if isinstance(pending, dict):
        changed.update(pending.keys())
    if isinstance(generated, dict):
        changed.update(generated.keys())
    if changed:
        listed = ", ".join(sorted(changed)[:8])
        more = "" if len(changed) <= 8 else f" (+{len(changed) - 8} more)"
        parts.append(f"Files changed: {listed}{more}.")

    if state.get("guardrail_failed"):
        feedback = state.get("validation_feedback")
        parts.append(f"Validation flagged an issue{f': {feedback}' if feedback else ''}.")

    errors = state.get("errors")
    if isinstance(errors, list) and errors:
        parts.append(f"{len(errors)} error(s) encountered: {errors[-1]}")

    if not parts:
        return "Task processed through the pipeline. No code changes were produced."
    return "\n".join(parts)


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
        self, session_id: str, payload: TaskPayload, execution_mode: str = "SEQUENTIAL"
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
            "execution_mode": execution_mode,
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

        # Phase 7.9.A.7.a — seed the session HITL policy from the user's persisted
        # preference (the in-graph evaluate_action() engine already enforces it).
        # Local import avoids any api↔core import-order coupling at module load.
        try:
            from api.system_settings import _read_settings as _read_sys_settings
            _pref_mode = str(_read_sys_settings().get("permission_mode", "default")).upper()
            if _pref_mode in ("DEFAULT", "PLAN", "AUTO"):
                initial_state["session_permission_mode"] = _pref_mode
        except Exception:  # noqa: BLE001 — preference seeding must never block a task
            pass

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}

        # 3. Ejecución del grafo con streaming de actualizaciones
        # Phase 7.9.B.12 — node completions flow on the dedicated pipeline-progress
        # channel (ephemeral stepper UI), NOT as chat tokens. merged_state flattens
        # the per-node deltas so we can synthesize one real assistant answer.
        final_output: dict = {}
        merged_state: dict = {}
        async for update in alienant_app.astream(
            cast(AIlienantGraphState, initial_state), config=config, stream_mode="updates"
        ):
            for node_name, node_output in update.items():
                step_id = (
                    node_output.get("current_step_id")
                    if isinstance(node_output, dict) else None
                )
                task = asyncio.create_task(
                    vfs_manager.broadcast_pipeline_step(session_id, node_name, step_id=step_id)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                if isinstance(node_output, dict):
                    merged_state.update(node_output)
            final_output.update(update)

        # 4. Stream the final answer — unless the graph suspended on HITL/ideation,
        # which already broadcast its own Socratic question via broadcast_token.
        if not merged_state.get("hitl_pending"):
            summary = _summarize_result(merged_state)
            await vfs_manager.broadcast_token(session_id, summary)
            await vfs_manager.broadcast_stream_end(session_id)

        return {
            "status": "success",
            "message": "Graph execution completed.",
            "session_id": session_id,
            "output": final_output,
        }
