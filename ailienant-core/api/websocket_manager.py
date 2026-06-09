# alienant-core/api/websocket_manager.py

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Set, cast

from fastapi import WebSocket
from pydantic import ValidationError, TypeAdapter

from api.ws_contracts import (
    WebSocketMessage,
    ServerTokenChunkEvent, TokenChunkPayload,
    ServerThinkingChunkEvent, ThinkingChunkPayload,   # Native Thinking
    ServerTelemetryEvent, TelemetryPayload,
    ServerGraphMutationEvent, GraphMutationPayload,
    ServerHITLApprovalRequestEvent, HITLApprovalRequestPayload, ProposedFile,
    ServerModelWarmupEvent, ModelWarmupPayload,
    ServerOomEngagedEvent, OomEngagedPayload,
    ServerIndexingProgressEvent, IndexingProgressPayload,
    ServerIndexingErrorEvent, IndexingErrorPayload,
    ServerVfsPatchApprovedEvent, VfsPatchApprovedPayload,
    ServerApplyWorkspaceEditEvent, ApplyWorkspaceEditPayload,
    ServerByomConfigAppliedEvent, ByomConfigAppliedPayload,
    ServerNattMessageEvent, NattMessagePayload,
    ServerNattTokenChunkEvent, NattTokenChunkPayload,
    ServerNattStreamEndEvent, NattStreamEndPayload,
    ServerPipelineStepEvent, PipelineStepPayload,
    ServerPlanDocumentEvent, PlanDocumentPayload,
    ServerStreamEndEvent,
    ServerInlineEditStartEvent, InlineEditStartPayload,
    ServerInlineEditDeltaEvent, InlineEditDeltaPayload,
    ServerInlineEditEndEvent, InlineEditEndPayload,
    ServerToolStartEvent, ToolStartPayload,
    ServerToolStreamChunkEvent, ToolStreamChunkPayload,
    ServerToolResultEvent, ToolResultPayload,
    ServerToolDepGraphEvent, ToolDepGraphPayload,
)

from core.telemetry_log import log_ws_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VFS_Manager")

# Cap the WS payload slice mirrored to the telemetry sink so a large diff-stream
# chunk never bloats a single log line (the sink also truncates defensively).
_TELEMETRY_LINE_CAP: int = 1_000


# session-disconnect hooks. Other modules
# (notably ``core.task_service`` for the tool-call registry) register a
# cleanup callback that runs whenever a WS session disconnects. This avoids
# coupling the manager to those modules directly and sidesteps the
# circular-import that bites if ``core.task_service`` is imported eagerly.
SessionCleanupHook = "Any"  # pyflakes — actual type is Callable[[str], object]
_SESSION_CLEANUP_HOOKS: List[Any] = []


def register_session_cleanup_hook(hook: Any) -> None:
    """Register a callable invoked on every WS disconnect with the client_id.

    Idempotent: the same hook is registered at most once. The callback may
    raise — the manager swallows exceptions so a buggy hook never blocks a
    disconnect from cleaning up the active-connections map.
    """
    if hook not in _SESSION_CLEANUP_HOOKS:
        _SESSION_CLEANUP_HOOKS.append(hook)

# Inbound flood guard — per-client token bucket. The Push model lets a save
# storm (or a misbehaving client) fire high-frequency telemetry-class events
# faster than the loop can absorb. Capacity mirrors the io_coalescer
# mass-threshold so a legitimate branch-switch burst passes, while a runaway
# flood is shed. Interactive events (chat/HITL/abort) are NEVER rate-limited.
_INBOUND_BUCKET_CAPACITY: float = 100.0
_INBOUND_REFILL_PER_S: float = 50.0

# =====================================================================
# TYPED ADAPTER (Pydantic V2)
# =====================================================================
# Compiled once at import time — reduces per-message validation latency.
ws_adapter: TypeAdapter[WebSocketMessage] = TypeAdapter(WebSocketMessage)


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
        # Multiplexing: one physical socket (keyed by its connection id) can serve
        # several sessions. register_alias points each session_id at that socket in
        # active_connections; this reverse index (connection id -> aliased session
        # ids) lets disconnect reap every alias the socket owned in O(k).
        self._aliases: Dict[str, Set[str]] = {}
        # Shutdown guard — set True by lifespan; gates new connect() calls
        self.shutting_down: bool = False
        # In-flight agent asyncio.Tasks; drained during graceful shutdown
        self.active_tasks: Set[asyncio.Task[Any]] = set()
        # HITL state — keyed by approval_id (UUID4), NOT session_id
        self._hitl_pending: Dict[str, asyncio.Event] = {}
        self._hitl_responses: Dict[str, Dict[str, Any]] = {}
        # write-pipeline acks, keyed by patch_id (UUID4 hex)
        self._patch_acks: Dict[str, asyncio.Event] = {}
        self._patch_ack_results: Dict[str, Dict[str, Any]] = {}
        # Reverse indexes (session_id -> in-flight request ids) so a disconnect
        # can reap any suspended waiter's buffers in O(k); k = the client's open
        # requests (normally tiny).
        self._client_pending_hitl: Dict[str, Set[str]] = {}
        self._client_pending_acks: Dict[str, Set[str]] = {}
        # Inbound flood guard — per-client token bucket (tokens + last-refill clock)
        self._inbound_tokens: Dict[str, float] = {}
        self._inbound_refill_at: Dict[str, float] = {}

    def has_client(self, session_id: str) -> bool:
        """True if a live WS client is connected for this session (gates disk writes)."""
        return session_id in self.active_connections

    def allow_inbound(self, client_id: str) -> bool:
        """Consume one inbound token for a high-frequency event; False if exhausted.

        A monotonic-clock token bucket per client, lazily initialized full so a
        fresh client never starts throttled. Callers gate ONLY telemetry-class
        events (file updates, IDE telemetry) on this — interactive events bypass
        it — so a flood is shed without ever starving chat or HITL.
        """
        now = time.monotonic()
        last = self._inbound_refill_at.get(client_id, now)
        tokens = min(
            _INBOUND_BUCKET_CAPACITY,
            self._inbound_tokens.get(client_id, _INBOUND_BUCKET_CAPACITY)
            + (now - last) * _INBOUND_REFILL_PER_S,
        )
        self._inbound_refill_at[client_id] = now
        if tokens < 1.0:
            self._inbound_tokens[client_id] = tokens
            return False
        self._inbound_tokens[client_id] = tokens - 1.0
        return True

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self, client_id: str, websocket: WebSocket, auth_token: Optional[str] = None
    ) -> bool:
        """Accept a WebSocket connection, optionally validating an ephemeral auth token.

        when auth_token is set, the very first message must be
        {"event_type": "auth", "token": "<token>"}. Constant-time comparison
        (secrets.compare_digest) prevents timing attacks on localhost.
        Returns True if the connection was accepted, False if rejected.
        """
        if self.shutting_down:
            await websocket.close(code=1001)
            return False
        await websocket.accept()
        if auth_token:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=3.0)
                msg = json.loads(raw)
                token_provided = msg.get("token", "")
                # secrets.compare_digest: constant-time — timing attacks are feasible
                # on localhost where network latency is near-zero.
                if msg.get("event_type") != "auth" or not secrets.compare_digest(
                    token_provided, auth_token
                ):
                    await websocket.close(code=4001)
                    logger.warning("🔐 WS auth rejected for client_id=%s", client_id)
                    return False
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
                await websocket.close(code=4001)
                logger.warning("🔐 WS auth timeout/invalid for client_id=%s", client_id)
                return False
        self.active_connections[client_id] = websocket
        logger.info(
            "🟢 IDE Conectado: %s. Total activos: %d",
            client_id,
            len(self.active_connections),
        )
        return True

    def register_alias(self, session_id: str, client_id: str) -> None:
        """Point ``session_id`` at the physical socket connected under ``client_id``.

        Multiplexing handshake: one socket (the per-window connection ``client_id``)
        serves many sessions. Each panel announces its ``session_id``; we alias it
        into ``active_connections`` so ``send_personal_message(session_id)`` reaches
        the socket, and record it under ``_aliases[client_id]`` so disconnect can
        reap it. Idempotent — a reconnect re-announce simply re-points the alias.
        """
        socket = self.active_connections.get(client_id)
        if socket is None:
            logger.warning(
                "register_alias: no live connection for client_id=%s (session=%s)",
                client_id, session_id,
            )
            return
        self.active_connections[session_id] = socket
        self._aliases.setdefault(client_id, set()).add(session_id)
        logger.info("🔗 Session %s registered on connection %s", session_id, client_id)

    def _reap_client_state(self, cid: str) -> None:
        """Drop all per-id buffers for ``cid`` and wake any suspended waiters.

        Used for the connection id and every session id aliased onto it: a
        mid-request disconnect (network flicker, IDE close) must never strand an
        event/result in the HITL or patch-ack maps. The result buffer is popped
        before set() so the woken coroutine resumes to an empty buffer and yields
        None.
        """
        self._inbound_tokens.pop(cid, None)
        self._inbound_refill_at.pop(cid, None)
        for approval_id in self._client_pending_hitl.pop(cid, set()):
            self._hitl_responses.pop(approval_id, None)
            hitl_event = self._hitl_pending.pop(approval_id, None)
            if hitl_event is not None:
                hitl_event.set()
        for patch_id in self._client_pending_acks.pop(cid, set()):
            self._patch_ack_results.pop(patch_id, None)
            patch_event = self._patch_acks.pop(patch_id, None)
            if patch_event is not None:
                patch_event.set()
        # fire every registered session-cleanup hook (e.g.,
        # TaskService.cleanup_session purges the tool-call registry). Hooks
        # are registered from main.py during startup; we never let a hook
        # exception derail the disconnect path.
        for hook in list(_SESSION_CLEANUP_HOOKS):
            try:
                hook(cid)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug("session-cleanup hook swallowed: %s", exc)

    def disconnect(self, client_id: str, websocket: Optional[WebSocket] = None) -> None:
        # Reconnect guard: if a fresh socket has already taken over this
        # connection id (reconnect under the same id), this is a stale close for
        # the dead socket — do nothing, or we would tear down the live
        # connection's aliases and in-flight waiters.
        current = self.active_connections.get(client_id)
        if websocket is not None and current is not None and current is not websocket:
            return
        # Every id this physical socket owned: its connection id + all aliased
        # session ids. Reap each so multiplexed sessions don't leak on close.
        owned = {client_id} | self._aliases.pop(client_id, set())
        for cid in owned:
            self.active_connections.pop(cid, None)
            self._reap_client_state(cid)
        logger.info(
            "🔴 IDE disconnected: %s (+%d session alias(es))", client_id, len(owned) - 1
        )

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    async def send_personal_message(self, client_id: str, event: WebSocketMessage) -> None:
        if client_id not in self.active_connections:
            return
        # Multiplexing: one physical socket carries every session's traffic, so
        # the client demultiplexes by data.session_id. Stamp the routing id (the
        # client_id this event is addressed to) into the payload here — the single
        # egress chokepoint — so EVERY event type is tagged without editing each
        # emitter. Single serialization pass: dump the model to a dict ONCE,
        # inject in memory, then encode ONCE (never dump→parse→re-dump, which
        # would triple-convert and spike CPU on long token streams).
        obj = event.model_dump(mode="json")
        data = obj.get("data")
        if isinstance(data, dict):
            data.setdefault("session_id", client_id)
        payload = json.dumps(obj, separators=(",", ":"))
        # Mirror to the live telemetry sink (O(1) enqueue, off-loop disk write).
        log_ws_payload("out", getattr(event, "event_type", "?"), client_id, payload[:_TELEMETRY_LINE_CAP])
        await self.active_connections[client_id].send_text(payload)

    # ------------------------------------------------------------------
    # Inbound validation (the shield)
    # ------------------------------------------------------------------

    async def validate_incoming(self, raw_json_string: str) -> Optional[WebSocketMessage]:
        """O(1) discriminated-union validation on every inbound message."""
        try:
            result = ws_adapter.validate_json(raw_json_string)
        except ValidationError as e:
            logger.error("⚠️ Inyección rechazada en la frontera: Payload malformado. Detalles: %s", e)
            return None
        # Mirror the accepted inbound event to the live telemetry sink.
        log_ws_payload(
            "in", getattr(result, "event_type", "?"), "?", raw_json_string[:_TELEMETRY_LINE_CAP]
        )
        return result

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

    async def broadcast_thinking_chunk(
        self, session_id: str, delta: str, token_count: int = 0
    ) -> None:
        """stream a native-reasoning delta to the Thought Box.

        A separate channel from ``broadcast_token`` (the answer stream) and
        ``broadcast_pipeline_step`` (node narration). Reuses the same
        ``send_personal_message`` plumbing.
        """
        await self.send_personal_message(
            session_id,
            ServerThinkingChunkEvent(
                data=ThinkingChunkPayload(
                    session_id=session_id, delta=delta, token_count=token_count
                )
            ),
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
                    # Callers (e.g. agents/coder.py) pass a plain str; the
                    # payload field is a closed Literal. Cast here rather than
                    # tightening the param type, which would cascade to the
                    # locked agent call sites.
                    new_status=cast(
                        Literal["pending", "in_progress", "completed", "failed"],
                        new_status,
                    ),
                    agent_name=agent_name,
                )
            ),
        )

    async def broadcast_model_warmup(
        self,
        session_id: str,
        model_name: str,
        is_local: bool,
        tier: Literal["small", "medium", "big"],
    ) -> None:
        """Signal the IDE that a model is being loaded/warmed up before inference."""
        await self.send_personal_message(
            session_id,
            ServerModelWarmupEvent(
                data=ModelWarmupPayload(
                    model_name=model_name,
                    is_local=is_local,
                    tier=tier,
                )
            ),
        )

    async def broadcast_oom_engaged(
        self,
        session_id: str,
        failed_model: str,
        fallback_model: str,
    ) -> None:
        """Warn the IDE that an OOM rescue swapped the local model for the cloud."""
        await self.send_personal_message(
            session_id,
            ServerOomEngagedEvent(
                data=OomEngagedPayload(
                    failed_model=failed_model,
                    fallback_model=fallback_model,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Workspace Indexing Progress
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

    async def broadcast_byom_config_applied(self, preset_id: str, preset_name: str) -> None:
        """Notify all connected clients that a BYOM preset was applied."""
        event = ServerByomConfigAppliedEvent(
            data=ByomConfigAppliedPayload(preset_id=preset_id, preset_name=preset_name)
        )
        payload = ws_adapter.dump_json(event).decode("utf-8")
        for ws in list(self.active_connections.values()):
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def broadcast_indexing_error(self, session_id: str, reason: str) -> None:
        """Signal to the IDE that indexing could not start due to a configuration error."""
        await self.send_personal_message(
            session_id,
            ServerIndexingErrorEvent(data=IndexingErrorPayload(reason=reason)),
        )

    # ------------------------------------------------------------------
    # Analyst pane + pipeline progress + stream end
    # ------------------------------------------------------------------

    async def send_natt_message(
        self, session_id: str, content: str, is_alert: bool = False
    ) -> None:
        """Deliver an analyst reply to the Natt canvas of a single client."""
        await self.send_personal_message(
            session_id,
            ServerNattMessageEvent(data=NattMessagePayload(content=content, is_alert=is_alert)),
        )

    async def broadcast_pipeline_step(
        self, session_id: str, node_name: str, step_id: Optional[int] = None
    ) -> None:
        """Report that a LangGraph node completed (progress UI, not chat content)."""
        await self.send_personal_message(
            session_id,
            ServerPipelineStepEvent(data=PipelineStepPayload(node_name=node_name, step_id=step_id)),
        )

    async def broadcast_plan_document(
        self, session_id: str, payload: PlanDocumentPayload
    ) -> None:
        """Send a finalized plan as structured data plus its chat pointer in one
        message, so the IDE renders the conversation bubble and the rich Plan panel
        in a single state transition (no two-message ordering race)."""
        await self.send_personal_message(
            session_id, ServerPlanDocumentEvent(data=payload)
        )

    async def broadcast_natt_token(self, session_id: str, token: str) -> None:
        """Stream a batched analyst token chunk to the Natt canvas (Phase 7.10.3)."""
        await self.send_personal_message(
            session_id,
            ServerNattTokenChunkEvent(data=NattTokenChunkPayload(token=token)),
        )

    async def broadcast_natt_stream_end(
        self, session_id: str, context_version: str = ""
    ) -> None:
        """Finalize the streamed analyst bubble + emit the G2 context version (Phase 7.10.3)."""
        await self.send_personal_message(
            session_id,
            ServerNattStreamEndEvent(
                data=NattStreamEndPayload(context_version=context_version or None)
            ),
        )

    async def broadcast_stream_end(
        self, session_id: str, checkpoint_id: Optional[str] = None,
    ) -> None:
        """Finalize the streaming assistant message bubble on the client.

        the optional ``checkpoint_id`` carries
        the L2-promoted snapshot id for the turn that just ended. The
        frontend attaches this to the last assistant ``Message`` so the
        per-message "↪ Branch from here" button can target it. Default
        ``None`` preserves the pre wire shape; all existing callers
        keep compiling unchanged.
        """
        data: Dict[str, Any] = {}
        if checkpoint_id:
            data["checkpoint_id"] = checkpoint_id
        await self.send_personal_message(session_id, ServerStreamEndEvent(data=data))

    # ------------------------------------------------------------------
    # Inline editor mutations (Cmd+K)
    # ------------------------------------------------------------------

    async def broadcast_inline_edit_start(
        self,
        session_id: str,
        edit_id: str,
        file_path: str,
        range_start: int,
        range_end: int,
    ) -> None:
        """Signal the host that an inline-edit stream is about to begin."""
        await self.send_personal_message(
            session_id,
            ServerInlineEditStartEvent(
                data=InlineEditStartPayload(
                    edit_id=edit_id,
                    session_id=session_id,
                    file_path=file_path,
                    range_start=range_start,
                    range_end=range_end,
                )
            ),
        )

    async def broadcast_inline_edit_delta(
        self,
        session_id: str,
        edit_id: str,
        kind: Literal["INSERT", "DELETE", "ABORT"],
        offset: int,
        length: int = 0,
        text: str = "",
    ) -> None:
        """Stream one typed mutation delta for the InlineMutationManager."""
        await self.send_personal_message(
            session_id,
            ServerInlineEditDeltaEvent(
                data=InlineEditDeltaPayload(
                    edit_id=edit_id,
                    session_id=session_id,
                    kind=kind,
                    offset=offset,
                    length=length,
                    text=text,
                )
            ),
        )

    async def broadcast_inline_edit_end(
        self,
        session_id: str,
        edit_id: str,
        success: bool,
        final_content: str = "",
        error: Optional[str] = None,
    ) -> None:
        """Finalize the inline-edit stream (success or abort)."""
        await self.send_personal_message(
            session_id,
            ServerInlineEditEndEvent(
                data=InlineEditEndPayload(
                    edit_id=edit_id,
                    session_id=session_id,
                    success=success,
                    final_content=final_content,
                    error=error,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Rich Tool Chips
    # ------------------------------------------------------------------

    async def broadcast_tool_start(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        args: Dict[str, Any],
        side_effect_free: bool,
        invoked_at: float,
    ) -> None:
        """Open a ToolChip on the IDE: pending status + the args header.

        Emits ``server_tool_start``. The frontend creates a placeholder chip
        keyed by ``tool_call_id``; subsequent chunk/result events update it.
        """
        await self.send_personal_message(
            session_id,
            ServerToolStartEvent(
                data=ToolStartPayload(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    side_effect_free=side_effect_free,
                    invoked_at=invoked_at,
                )
            ),
        )

    async def broadcast_tool_stream_chunk(
        self,
        session_id: str,
        tool_call_id: str,
        chunk: str,
        is_stderr: bool = False,
    ) -> None:
        """Append a chunk of stdout/stderr to the ToolChip mini-terminal.

        For the sandbox-adapter path this fires exactly once with the full
        truncated body. A future streaming adapter (e.g., live PTY) can emit
        many; the frontend appends them to ``output_lines`` in order.
        """
        await self.send_personal_message(
            session_id,
            ServerToolStreamChunkEvent(
                data=ToolStreamChunkPayload(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    chunk=chunk,
                    is_stderr=is_stderr,
                )
            ),
        )

    async def broadcast_tool_result(
        self,
        session_id: str,
        tool_call_id: str,
        status: Literal["success", "error"],
        exit_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Finalize the ToolChip: status badge flips, retry button enables.

        ``status`` is the literal ``"success"`` or ``"error"``. ``exit_code``
        is None when the failure was a Python exception (no process exited).
        """
        await self.send_personal_message(
            session_id,
            ServerToolResultEvent(
                data=ToolResultPayload(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    status=status,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                )
            ),
        )

    async def broadcast_tool_dep_graph(
        self,
        session_id: str,
        tool_call_id: str,
        nodes: List[Dict[str, str]],
        edges: List[Dict[str, str]],
    ) -> None:
        """Attach an optional dependency-graph blob to an existing ToolChip.

        The frontend renders it in the chip's "graph" tab as a CSS/SVG
        disclosure tree. Nodes are ``{id, label}``; edges are ``{from, to}``.
        """
        await self.send_personal_message(
            session_id,
            ServerToolDepGraphEvent(
                data=ToolDepGraphPayload(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    nodes=nodes,
                    edges=edges,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Time-Travel Debugging (Thread Branching)
    # ------------------------------------------------------------------

    async def broadcast_session_branched(
        self,
        parent_session_id: str,
        new_session_id: str,
        from_checkpoint_id: str,
    ) -> None:
        """Notify the frontend that a new session was minted from a fork.

        Dispatched to BOTH the parent session (so the open chat can refresh
        its sidebar and dismiss any pending picker overlay) AND the new
        session (so a re-attached client sees the branch immediately). The
        envelope is identical; the frontend de-dupes by ``new_session_id``.
        """
        from api.ws_contracts import (
            ServerSessionBranchedEvent,
            ServerSessionBranchedPayload,
        )
        envelope = ServerSessionBranchedEvent(
            data=ServerSessionBranchedPayload(
                parent_session_id=parent_session_id,
                new_session_id=new_session_id,
                from_checkpoint_id=from_checkpoint_id,
            )
        )
        await self.send_personal_message(parent_session_id, envelope)
        # New-session broadcast is best-effort: a brand-new sessionId likely
        # has no active connection yet (the host attaches AFTER opening it).
        try:
            await self.send_personal_message(new_session_id, envelope)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Delivery acknowledgements (Stop / HITL)
    # ------------------------------------------------------------------

    async def broadcast_abort_ack(self, session_id: str, signalled: bool) -> None:
        """Confirm a client_abort_mesh was resolved (signalled = a task was cancelled)."""
        from api.ws_contracts import AbortAckPayload, ServerAbortAckEvent

        await self.send_personal_message(
            session_id,
            ServerAbortAckEvent(
                data=AbortAckPayload(session_id=session_id, signalled=signalled)
            ),
        )

    async def broadcast_hitl_ack(
        self, session_id: str, approval_id: str, ok: bool
    ) -> None:
        """Confirm a client_hitl_response was applied (closes the orphan-response gap)."""
        from api.ws_contracts import HitlAckPayload, ServerHitlAckEvent

        await self.send_personal_message(
            session_id,
            ServerHitlAckEvent(data=HitlAckPayload(approval_id=approval_id, ok=ok)),
        )

    # ------------------------------------------------------------------
    # VFS Patch Approved (IPC Bridge)
    # ------------------------------------------------------------------

    async def emit_vfs_patch_approved(
        self,
        session_id: str,
        file_path: str,
        unified_diff: str,
        mode: Literal["autonomous", "supervision"],
    ) -> None:
        """Notify the IDE that a patch was committed to the RAM-VFS."""
        await self.send_personal_message(
            session_id,
            ServerVfsPatchApprovedEvent(
                data=VfsPatchApprovedPayload(
                    file_path=file_path,
                    unified_diff=unified_diff,
                    mode=mode,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Write pipeline: dispatch applyEdit + await host ack
    # ------------------------------------------------------------------

    async def emit_apply_workspace_edit(
        self, session_id: str, payload: ApplyWorkspaceEditPayload
    ) -> None:
        """Dispatch a set of file edits to the VS Code host for applyEdit + save."""
        await self.send_personal_message(
            session_id, ServerApplyWorkspaceEditEvent(data=payload)
        )

    async def wait_patch_ack(
        self, patch_id: str, session_id: str, timeout: float = 30.0
    ) -> Optional[Dict[str, Any]]:
        """Suspend until the host acks patch_id (client_patch_applied) or timeout fires."""
        event = asyncio.Event()
        self._patch_acks[patch_id] = event
        self._client_pending_acks.setdefault(session_id, set()).add(patch_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._patch_ack_results.pop(patch_id, None)
        except asyncio.TimeoutError:
            logger.warning("⏱️ patch ack timeout (patch_id=%s)", patch_id)
            return None
        finally:
            self._patch_acks.pop(patch_id, None)
            pending = self._client_pending_acks.get(session_id)
            if pending is not None:
                pending.discard(patch_id)
                if not pending:
                    self._client_pending_acks.pop(session_id, None)

    def resolve_patch_ack(self, patch_id: str, result: Dict[str, Any]) -> None:
        """Called from the WS receive loop on client_patch_applied — unblocks the waiter."""
        event = self._patch_acks.get(patch_id)
        if event is None:
            # Late ack: the waiter already timed out or was cancelled. patch_id is
            # single-use, so no future waiter can ever consume this — drop it
            # rather than orphaning an entry in the result buffer.
            logger.warning("⚠️ patch ack for unknown patch_id: %s", patch_id)
            return
        self._patch_ack_results[patch_id] = result
        event.set()

    # ------------------------------------------------------------------
    # HITL — Human-in-the-Loop suspension
    # ------------------------------------------------------------------

    async def request_human_approval(
        self,
        session_id: str,
        action_description: str,
        proposed_content: Optional[str] = None,
        timeout_s: Optional[float] = 300.0,
        request_kind: Optional[str] = None,
        proposed_files: Optional[List[ProposedFile]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Suspend the calling coroutine until the human responds or the timeout fires.

        Each call generates a unique approval_id (UUID4). This ID is sent to the
        client inside the request event and must be echoed back in the response,
        preventing cross-talk when multiple approval requests are in-flight on
        the same session.

        ``request_kind`` — optional classifier string (e.g.
        ``"BUDGET_OVERFLOW"``, ``"FILE_WRITE"``) that the native-HITL toast bridge
        uses to choose severity and title. Default None preserves the pre
        wire shape; unknown kinds fall back to info-level on the frontend.

        ``timeout_s`` of None waits indefinitely — used for an interactive edit
        approval, where a wall-clock deadline would leave the operator returning
        to a dead card. The wait is still bounded by the connection: a disconnect
        reaps the pending event (see ``_reap_client_state``) and wakes the waiter,
        which then resolves to None. Bounded callers (FinOps, sandbox tiers) keep
        passing a float.

        Returns {"approved": bool, "comment": str|None} or None on timeout.
        """
        approval_id = str(uuid.uuid4())
        event = asyncio.Event()
        self._hitl_pending[approval_id] = event
        self._client_pending_hitl.setdefault(session_id, set()).add(approval_id)

        try:
            await self.send_personal_message(
                session_id,
                ServerHITLApprovalRequestEvent(
                    data=HITLApprovalRequestPayload(
                        session_id=session_id,
                        approval_id=approval_id,
                        action_description=action_description,
                        proposed_content=proposed_content,
                        request_kind=request_kind,
                        proposed_files=proposed_files,
                    )
                ),
            )
            if timeout_s is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout_s)
            decision: Optional[Dict[str, Any]] = self._hitl_responses.pop(approval_id, None)
        except asyncio.TimeoutError:
            logger.warning("⏱️ HITL timeout for session %s (approval_id=%s)", session_id, approval_id)
            decision = None
        finally:
            self._hitl_pending.pop(approval_id, None)
            pending = self._client_pending_hitl.get(session_id)
            if pending is not None:
                pending.discard(approval_id)
                if not pending:
                    self._client_pending_hitl.pop(session_id, None)

        # append one immutable row to the HITL audit chain. Approved,
        # rejected and timeout are all logged (no gap-attack surface). Best-effort:
        # an audit-write failure must never break the HITL round-trip.
        resolution = (
            "timeout" if decision is None
            else "approved" if decision.get("approved") else "rejected"
        )
        try:
            from core.audit import log_audit_event  # deferred — avoids import cycle
            await log_audit_event(
                session_id=session_id,
                action_description=action_description,
                proposed_content=proposed_content,
                resolution=resolution,
                resolution_comment=None if decision is None else decision.get("comment"),
                audit_id=approval_id,
            )
        except Exception:  # noqa: BLE001 — audit failure must never break HITL
            logger.error("HITL audit-log write failed for approval_id=%s", approval_id)

        return decision

    def resolve_human_approval(
        self,
        approval_id: str,
        approved: bool,
        comment: Optional[str] = None,
        modified_content: Optional[str] = None,
    ) -> None:
        """
        Called from the WS receive loop when client_hitl_response arrives.
        Stores the response and unblocks the waiting coroutine.
        Silently ignores unknown approval_ids (e.g. late responses after timeout).

        modified_content carries an optional edited payload from the
        HITL card's edit mode — consumed by the write pipeline for single-file patches.
        """
        event = self._hitl_pending.get(approval_id)
        if event is None:
            # Late response: the waiter already timed out or was cancelled.
            # approval_id is single-use, so no future waiter can consume this —
            # drop it rather than orphaning an entry in the response buffer.
            logger.warning("⚠️ HITL response received for unknown approval_id: %s", approval_id)
            return
        self._hitl_responses[approval_id] = {
            "approved": approved,
            "comment": comment,
            "modified_content": modified_content,
        }
        event.set()


# Global singleton
vfs_manager = ConnectionManager()
