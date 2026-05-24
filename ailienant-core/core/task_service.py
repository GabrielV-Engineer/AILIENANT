import asyncio
import os
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, cast
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment, AIlienantGraphState
from brain.engine import alienant_app
from api.websocket_manager import vfs_manager
from tools.llm_gateway import LLMGateway
from langchain_core.runnables import RunnableConfig
import logging

logger = logging.getLogger(__name__)

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: set = set()


# Phase 7.9.B.13 — persona for the live main-chat completion (direct BYOM call).
_CHAT_SYSTEM_PROMPT: str = (
    "You are AILIENANT, an expert AI coding assistant embedded in the user's IDE. "
    "Answer the user's request directly and concisely. When the task involves code, "
    "provide correct, idiomatic snippets and explain the key decisions briefly. "
    "If the request is ambiguous, state the assumption you are making and proceed."
)

# Phase 7.9.B.15 — short-term session memory + GraphRAG injection.
# In-memory and ephemeral by design (matches the "/context clear" UX wording);
# keyed by the stable session_id (== WS client_id == X-Task-ID).
_MAX_HISTORY_MESSAGES: int = 24      # ~12 turns retained per conversation
_RAG_TOP_K: int = 5
_conversations: Dict[str, List[Dict[str, str]]] = {}


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

        # 4. Stream a real answer from the active BYOM chat model — unless the graph
        # suspended on HITL/ideation, which already broadcast its own question.
        if not merged_state.get("hitl_pending"):
            await self._stream_chat_answer(
                session_id, payload.task_prompt, payload.project_id
            )

        return {
            "status": "success",
            "message": "Graph execution completed.",
            "session_id": session_id,
            "output": final_output,
        }

    def _append_history(self, session_id: str, role: str, content: str) -> None:
        """Append a turn message to the session's short-term memory (bounded)."""
        history = _conversations.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        if len(history) > _MAX_HISTORY_MESSAGES:
            del history[: len(history) - _MAX_HISTORY_MESSAGES]

    def clear_conversation(self, session_id: str) -> None:
        """Drop the session's short-term memory (wired to /context clear)."""
        _conversations.pop(session_id, None)

    async def _build_rag_context(self, task_prompt: str, project_id: Optional[str]) -> str:
        """Fetch top-k LanceDB snippets for the prompt and format them for the system prompt.

        Returns '' when no project, no index, or any failure — RAG is best-effort
        and must never block or break a chat turn.
        """
        if not project_id:
            return ""
        try:
            from core.memory.semantic_memory import SemanticMemoryManager
            snippets = await SemanticMemoryManager().search_snippets(
                task_prompt, workspace_hash=project_id, k=_RAG_TOP_K
            )
        except Exception as exc:  # noqa: BLE001 — RAG fetch is non-fatal
            logger.debug("RAG context fetch failed (non-fatal): %s", exc)
            return ""
        if not snippets:
            return ""
        blocks = "\n\n".join(f"### {path}\n{snip}" for path, snip in snippets if snip)
        if not blocks:
            return ""
        return (
            "\n\n# Relevant workspace context (GraphRAG)\n"
            "Code excerpts from the user's project that may be relevant. "
            "Use them when helpful; ignore them otherwise.\n\n" + blocks
        )

    async def _stream_chat_answer(
        self, session_id: str, task_prompt: str, project_id: Optional[str] = None
    ) -> None:
        """Stream a live completion from the active BYOM chat model to the IDE.

        Injects short-term session memory + GraphRAG snippets. Persists the turn
        only on a successful, non-empty reply. Always finalizes with
        broadcast_stream_end; on failure it broadcasts an actionable message.
        """
        system_content = _CHAT_SYSTEM_PROMPT + await self._build_rag_context(
            task_prompt, project_id
        )
        history = _conversations.get(session_id, [])
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_content},
            *history,
            {"role": "user", "content": task_prompt},
        ]
        reply_parts: List[str] = []
        try:
            async for delta in LLMGateway.astream_byom(
                messages, tier="medium", session_id=session_id
            ):
                reply_parts.append(delta)
                await vfs_manager.broadcast_token(session_id, delta)
            reply = "".join(reply_parts)
            if reply:
                # Persist only completed turns so a failure never poisons memory.
                self._append_history(session_id, "user", task_prompt)
                self._append_history(session_id, "assistant", reply)
            else:
                await vfs_manager.broadcast_token(
                    session_id,
                    "No response was produced. Check that a BYOM preset is active and "
                    "its model is reachable (Dashboard → BYOM).",
                )
        except Exception as exc:  # noqa: BLE001 — a chat failure must never crash the task
            logger.warning("Live chat completion failed: %s", exc)
            await vfs_manager.broadcast_token(
                session_id,
                "I couldn't reach the configured model. Activate a BYOM preset "
                "(Dashboard → BYOM) and make sure its engine is running, then try again.",
            )
        finally:
            await vfs_manager.broadcast_stream_end(session_id)
