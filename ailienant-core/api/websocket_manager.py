# alienant-core/api/websocket_manager.py

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket
from pydantic import ValidationError, TypeAdapter

from ws_contracts import (
    WebSocketMessage,
    ServerTokenChunkEvent, TokenChunkPayload,
    ServerTelemetryEvent, TelemetryPayload,
    ServerGraphMutationEvent, GraphMutationPayload,
    ServerHITLApprovalRequestEvent, HITLApprovalRequestPayload,
    ServerModelWarmupEvent, ModelWarmupPayload,
    ServerIndexingProgressEvent, IndexingProgressPayload,
    ServerVfsPatchApprovedEvent, VfsPatchApprovedPayload,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VFS_Manager")

# =====================================================================
# TYPED ADAPTER (Pydantic V2)
# =====================================================================
# Compiled once at import time — reduces per-message validation latency.
ws_adapter = TypeAdapter(WebSocketMessage)


class ConnectionManager:
    """
    Singleton WebSocket nerve center.

    Responsibilities:
    - Manage active IDE connections keyed by client_id.
    - Emit typed server→client events (token stream, telemetry, graph mutations).
    - Implement HITL suspension via per-request asyncio.Event keyed by approval_id
      (not session_id) to prevent cross-talk between concurrent approval requests
      on the same session.
    """

    def __init__(self) -> None:
        self.active_connections: Dict[str, WebSocket] = {}
        # Shutdown guard — set True by lifespan; gates new connect() calls
        self.shutting_down: bool = False
        # In-flight agent asyncio.Tasks; drained during graceful shutdown
        self.active_tasks: Set[asyncio.Task[Any]] = set()
        # HITL state — keyed by approval_id (UUID4), NOT session_id
        self._hitl_pending: Dict[str, asyncio.Event] = {}
        self._hitl_responses: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, client_id: str, websocket: WebSocket) -> None:
        if self.shutting_down:
            await websocket.close(code=1001)
            return
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(
            "🟢 IDE Conectado: %s. Total activos: %d",
            client_id,
            len(self.active_connections),
        )

    def disconnect(self, client_id: str) -> None:
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info("🔴 IDE Desconectado: %s", client_id)

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    async def send_personal_message(self, client_id: str, event: WebSocketMessage) -> None:
        if client_id not in self.active_connections:
            return
        payload = ws_adapter.dump_json(event).decode("utf-8")
        await self.active_connections[client_id].send_text(payload)

    # ------------------------------------------------------------------
    # Inbound validation (the shield)
    # ------------------------------------------------------------------

    async def validate_incoming(self, raw_json_string: str) -> Optional[WebSocketMessage]:
        """O(1) discriminated-union validation on every inbound message."""
        try:
            return ws_adapter.validate_json(raw_json_string)
        except ValidationError as e:
            logger.error("⚠️ Inyección rechazada en la frontera: Payload malformado. Detalles: %s", e)
            return None

    # ------------------------------------------------------------------
    # Outbound emit helpers (server → client)
    # ------------------------------------------------------------------

    async def broadcast_token(
        self, session_id: str, token: str, step_id: Optional[int] = None
    ) -> None:
        """Stream a single LLM output token to the IDE."""
        await self.send_personal_message(
            session_id,
            ServerTokenChunkEvent(data=TokenChunkPayload(token=token, step_id=step_id)),
        )

    async def send_telemetry(
        self,
        session_id: str,
        routing_decision: str,
        css_total: float,
        task_complexity_index: float,
        is_red_alert: bool,
    ) -> None:
        """Push routing telemetry snapshot to the IDE status bar."""
        await self.send_personal_message(
            session_id,
            ServerTelemetryEvent(
                data=TelemetryPayload(
                    session_id=session_id,
                    routing_decision=routing_decision,
                    css_total=css_total,
                    task_complexity_index=task_complexity_index,
                    is_red_alert=is_red_alert,
                )
            ),
        )

    async def emit_graph_mutation(
        self,
        session_id: str,
        step_number: int,
        new_status: str,
        agent_name: Optional[str] = None,
    ) -> None:
        """Notify the IDE that a WBS step changed status."""
        await self.send_personal_message(
            session_id,
            ServerGraphMutationEvent(
                data=GraphMutationPayload(
                    step_number=step_number,
                    new_status=new_status,
                    agent_name=agent_name,
                )
            ),
        )

    async def broadcast_model_warmup(
        self,
        session_id: str,
        model_name: str,
        is_local: bool,
        tier: str,
    ) -> None:
        """Signal the IDE that a model is being loaded/warmed up before inference."""
        await self.send_personal_message(
            session_id,
            ServerModelWarmupEvent(
                data=ModelWarmupPayload(
                    model_name=model_name,
                    is_local=is_local,
                    tier=tier,  # type: ignore[arg-type]
                )
            ),
        )

    # ------------------------------------------------------------------
    # Phase 2.5 — Workspace Indexing Progress
    # ------------------------------------------------------------------

    async def broadcast_indexing_progress(
        self, session_id: str, current: int, total: int
    ) -> None:
        """Stream workspace indexing progress to the IDE progress bar."""
        pct = round(current / total * 100.0 if total > 0 else 0.0, 1)
        await self.send_personal_message(
            session_id,
            ServerIndexingProgressEvent(
                data=IndexingProgressPayload(current=current, total=total, percentage=pct)
            ),
        )

    async def broadcast_indexing_complete(self, session_id: str) -> None:
        """Signal 100% indexing completion to the IDE."""
        await self.broadcast_indexing_progress(session_id, current=1, total=1)

    # ------------------------------------------------------------------
    # Phase 2.22.4 — VFS Patch Approved (IPC Bridge)
    # ------------------------------------------------------------------

    async def emit_vfs_patch_approved(
        self,
        session_id: str,
        file_path: str,
        unified_diff: str,
        mode: str,
    ) -> None:
        """Notify the IDE that a patch was committed to the RAM-VFS."""
        await self.send_personal_message(
            session_id,
            ServerVfsPatchApprovedEvent(
                data=VfsPatchApprovedPayload(
                    file_path=file_path,
                    unified_diff=unified_diff,
                    mode=mode,  # type: ignore[arg-type]
                )
            ),
        )

    # ------------------------------------------------------------------
    # HITL — Human-in-the-Loop suspension
    # ------------------------------------------------------------------

    async def request_human_approval(
        self,
        session_id: str,
        action_description: str,
        proposed_content: Optional[str] = None,
        timeout_s: float = 300.0,
    ) -> Optional[dict]:
        """
        Suspend the calling coroutine until the human responds or the timeout fires.

        Each call generates a unique approval_id (UUID4). This ID is sent to the
        client inside the request event and must be echoed back in the response,
        preventing cross-talk when multiple approval requests are in-flight on
        the same session.

        Returns {"approved": bool, "comment": str|None} or None on timeout.
        """
        approval_id = str(uuid.uuid4())
        event = asyncio.Event()
        self._hitl_pending[approval_id] = event

        try:
            await self.send_personal_message(
                session_id,
                ServerHITLApprovalRequestEvent(
                    data=HITLApprovalRequestPayload(
                        session_id=session_id,
                        approval_id=approval_id,
                        action_description=action_description,
                        proposed_content=proposed_content,
                    )
                ),
            )
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            return self._hitl_responses.pop(approval_id, None)
        except asyncio.TimeoutError:
            logger.warning("⏱️ HITL timeout for session %s (approval_id=%s)", session_id, approval_id)
            return None
        finally:
            self._hitl_pending.pop(approval_id, None)

    def resolve_human_approval(
        self, approval_id: str, approved: bool, comment: Optional[str] = None
    ) -> None:
        """
        Called from the WS receive loop when client_hitl_response arrives.
        Stores the response and unblocks the waiting coroutine.
        Silently ignores unknown approval_ids (e.g. late responses after timeout).
        """
        self._hitl_responses[approval_id] = {"approved": approved, "comment": comment}
        if approval_id in self._hitl_pending:
            self._hitl_pending[approval_id].set()
        else:
            logger.warning("⚠️ HITL response received for unknown approval_id: %s", approval_id)


# Global singleton
vfs_manager = ConnectionManager()
