# alienant-core/core/ws_contracts.py

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional, Union

# =====================================================================
# 1. PAYLOADS DE LOS EVENTOS
# =====================================================================


class FileUpdatePayload(BaseModel):
    """Payload para cuando el IDE envía código fresco al Backend."""

    filepath: str = Field(..., description="Ruta absoluta del archivo en el IDE")
    content: str = Field(..., description="Contenido actual en el buffer de VS Code")
    # Este es el corazón del OCC que definimos en state.py
    document_version_id: str = Field(..., description="Timestamp o Hash del IDE")


class CodeProposalPayload(BaseModel):
    """Payload para cuando la IA quiere escribir en el IDE."""

    filepath: str
    proposed_content: str
    # La IA envía el ID sobre el cual basó sus cálculos
    base_document_version_id: str
    agent_signature: str = Field(..., description="Agente que propone (ej. LogicAgent)")


class StatusPayload(BaseModel):
    """Payload efímero para actualizar la UI de la extensión."""

    agent_name: str
    status_message: str
    is_error: bool = False


# =====================================================================
# 2. ENVOLTORIOS DE EVENTOS (Tagged Unions)
# =====================================================================


class ClientFileUpdateEvent(BaseModel):
    # La clave 'event_type' es nuestro discriminador constante
    event_type: Literal["client_file_update"] = "client_file_update"
    data: FileUpdatePayload


class ServerCodeProposalEvent(BaseModel):
    event_type: Literal["server_code_proposal"] = "server_code_proposal"
    data: CodeProposalPayload


class ServerStatusEvent(BaseModel):
    event_type: Literal["server_status"] = "server_status"
    data: StatusPayload


# =====================================================================
# 3. PHASE 1.4 — BIDIRECTIONAL STREAMING & HITL CONTRACTS
# =====================================================================


class TokenChunkPayload(BaseModel):
    """Single LLM output token streamed to the IDE."""

    token: str
    step_id: Optional[int] = None   # WBS step currently executing, if known


class ThinkingChunkPayload(BaseModel):
    """Phase 9 (ADR-707) — a native-reasoning ("thinking") delta streamed to the
    IDE's collapsible Thought Box.

    Distinct from ``TokenChunkPayload`` (the answer stream) and from
    ``PipelineStepPayload`` (synthesized node narration, ADR-702). ``delta`` is
    a raw reasoning-token fragment; ``token_count`` is the cumulative thinking
    token count for the current turn (drives the live "N tokens" telemetry).
    Display-only: the frontend renders it sanitized and NEVER feeds it back into
    the agent loop.
    """

    session_id: str
    delta: str
    token_count: int = 0


class TelemetryPayload(BaseModel):
    """Routing telemetry snapshot for the IDE status bar."""

    session_id: str
    routing_decision: str           # "LOCAL_SMALL" | "LOCAL_BIG" | "CLOUD"
    css_total: float
    task_complexity_index: float
    is_red_alert: bool


class GraphMutationPayload(BaseModel):
    """WBS step status transition emitted by the Orchestrator."""

    step_number: int
    new_status: Literal["pending", "in_progress", "completed", "failed"]
    agent_name: Optional[str] = None


class PlannerModeTogglePayload(BaseModel):
    """Client request to switch Planner-only mode on or off."""

    active: bool


class HITLApprovalRequestPayload(BaseModel):
    """Backend suspends and asks the human to approve a proposed action."""

    session_id: str
    approval_id: str                    # UUID4 — unique per request; client must echo this back
    action_description: str
    proposed_content: Optional[str] = None
    # Phase 7.11.7 (ADR-706 §4.5f) — additive classifier so the native-toast
    # surface can choose severity (info vs warning) and emit a short title.
    # Backward-compatible: pre-7.11.7 payloads omit this field and pydantic
    # treats it as None. Known kinds today: BUDGET_OVERFLOW, TOKEN_SPIKE,
    # SANDBOX_DEGRADED_EXEC, DRIFT_DETECTED, BUDGET_CEILING, RESOURCE_CONTENTION,
    # FILE_WRITE. Kept as a plain string (not a Literal[...]) so future emitters
    # can add new kinds without a schema bump; unknown kinds fall back to
    # info-level on the frontend.
    request_kind: Optional[str] = None


class HITLResponsePayload(BaseModel):
    """Client response to a pending HITL approval request."""

    approval_id: str                    # Must match the approval_id from the request
    approved: bool
    comment: Optional[str] = None
    # Phase 7.9.B.18 — optional edited payload from the HITL card's edit mode.
    # For a single-file patch, this overrides the proposed content before apply.
    modified_content: Optional[str] = None


# --- Server → Client Events ---

class ServerTokenChunkEvent(BaseModel):
    event_type: Literal["server_token_chunk"] = "server_token_chunk"
    data: TokenChunkPayload


class ServerThinkingChunkEvent(BaseModel):
    # Phase 9 (ADR-707) — Native Thinking reasoning delta.
    event_type: Literal["server_thinking_chunk"] = "server_thinking_chunk"
    data: ThinkingChunkPayload


class ServerTelemetryEvent(BaseModel):
    event_type: Literal["server_telemetry"] = "server_telemetry"
    data: TelemetryPayload


class ServerGraphMutationEvent(BaseModel):
    event_type: Literal["server_graph_mutation"] = "server_graph_mutation"
    data: GraphMutationPayload


class ServerHITLApprovalRequestEvent(BaseModel):
    event_type: Literal["server_hitl_approval_request"] = "server_hitl_approval_request"
    data: HITLApprovalRequestPayload


# --- Client → Server Events ---

class ClientPlannerModeToggleEvent(BaseModel):
    event_type: Literal["client_planner_mode_toggle"] = "client_planner_mode_toggle"
    data: PlannerModeTogglePayload


class ClientHITLResponseEvent(BaseModel):
    event_type: Literal["client_hitl_response"] = "client_hitl_response"
    data: HITLResponsePayload


# =====================================================================
# 5. OCC — OPTIMISTIC CONCURRENCY CONTROL (Phase 1.5)
# =====================================================================


class ConcurrencyConflictPayload(BaseModel):
    """Client reports a file version conflict detected during inference."""

    filepath: str = Field(..., description="Absolute path of the conflicting file")
    expected_version: int = Field(..., description="Version at task submission")
    actual_version: int = Field(..., description="Current version after user edited the file")


class ClientConcurrencyConflictEvent(BaseModel):
    event_type: Literal["client_concurrency_conflict"] = "client_concurrency_conflict"
    data: ConcurrencyConflictPayload


# =====================================================================
# 6. PHASE 2.2 — MODEL WARMUP SIGNAL
# =====================================================================


class ModelWarmupPayload(BaseModel):
    """Emitted before a local model inference call that may require warmup time."""

    model_name: str
    is_local: bool
    tier: Literal["small", "medium", "big"]


class ServerModelWarmupEvent(BaseModel):
    event_type: Literal["server_model_warmup"] = "server_model_warmup"
    data: ModelWarmupPayload


# =====================================================================
# 7. PHASE 2.5 — LAZY WORKSPACE INDEXING CONTRACTS
# =====================================================================


class WorkspaceInitPayload(BaseModel):
    """Client announces the workspace root path for lazy background indexing."""
    workspace_root: str
    project_id: str
    workspace_pid: Optional[int] = None  # Phase 4.4 — VS Code window PID


class IndexingProgressPayload(BaseModel):
    """Server progress broadcast during lazy workspace indexing."""
    current: int
    total: int
    percentage: float


class ClientWorkspaceInitEvent(BaseModel):
    event_type: Literal["client_workspace_init"] = "client_workspace_init"
    data: WorkspaceInitPayload


class ServerIndexingProgressEvent(BaseModel):
    event_type: Literal["server_indexing_progress"] = "server_indexing_progress"
    data: IndexingProgressPayload


class IndexingErrorPayload(BaseModel):
    """Server signals that indexing cannot start due to a configuration error."""
    reason: str


class ServerIndexingErrorEvent(BaseModel):
    event_type: Literal["server_indexing_error"] = "server_indexing_error"
    data: IndexingErrorPayload


class ByomConfigAppliedPayload(BaseModel):
    """Emitted after a BYOM preset is saved and applied to config.yaml."""
    preset_id: str
    preset_name: str


class ServerByomConfigAppliedEvent(BaseModel):
    event_type: Literal["server_byom_config_applied"] = "server_byom_config_applied"
    data: ByomConfigAppliedPayload


# =====================================================================
# 8. PHASE 2.1.13 — FILE DELETE / UNLINK EVENTS
# =====================================================================


class FileDeletePayload(BaseModel):
    """Payload for IDE file deletion (unlink) events."""
    filepath: str = Field(..., description="Absolute path of the deleted file")
    project_id: str = Field(default="", description="Project scope for DB purge")


class ClientFileDeleteEvent(BaseModel):
    event_type: Literal["client_file_delete"] = "client_file_delete"
    data: FileDeletePayload


# =====================================================================
# 9. PHASE 2.22.4 — VFS PATCH APPROVED (IPC Bridge)
# =====================================================================


class VfsPatchApprovedPayload(BaseModel):
    """Server notifies the IDE that a patch was committed to the RAM-VFS."""

    file_path: str
    unified_diff: str
    mode: Literal["autonomous", "supervision"]


class ServerVfsPatchApprovedEvent(BaseModel):
    event_type: Literal["server_vfs_patch_approved"] = "server_vfs_patch_approved"
    data: VfsPatchApprovedPayload


# =====================================================================
# 9b. PHASE 7.9.B.18 — ENTERPRISE WRITE PIPELINE (VS Code applyEdit bridge)
# =====================================================================


class WorkspaceEditItem(BaseModel):
    """One file edit dispatched to the VS Code applyEdit actuator."""

    file_path: str                       # absolute or workspace-relative (host resolves)
    new_content: str                     # full replacement content
    base_hash: Optional[str] = None      # sha256(pre-edit, EOL-normalized) for the stale guard


class ApplyWorkspaceEditPayload(BaseModel):
    """Server → host: apply a set of file edits atomically via vscode.workspace.applyEdit."""

    patch_id: str
    save: bool = True
    edits: list[WorkspaceEditItem]


class ServerApplyWorkspaceEditEvent(BaseModel):
    event_type: Literal["server_apply_workspace_edit"] = "server_apply_workspace_edit"
    data: ApplyWorkspaceEditPayload


class PatchAppliedPayload(BaseModel):
    """Host → server: result ack for a dispatched applyEdit."""

    patch_id: str
    ok: bool
    applied_files: list[str] = Field(default_factory=list)
    stale_files: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class ClientPatchAppliedEvent(BaseModel):
    event_type: Literal["client_patch_applied"] = "client_patch_applied"
    data: PatchAppliedPayload


# =====================================================================
# 10. PHASE 3.4.1 — INTELLIGENCE PROFILE / MASTER TOGGLE EVENTS
# =====================================================================


class MasterTogglePayload(BaseModel):
    enabled: bool


class ClientMasterToggleEvent(BaseModel):
    event_type: Literal["client_master_toggle"] = "client_master_toggle"
    data: MasterTogglePayload


class ProfileChangePayload(BaseModel):
    profile: Literal["Medium", "Big", "Cloud", "Hybrid"]


class ClientProfileChangeEvent(BaseModel):
    event_type: Literal["client_profile_change"] = "client_profile_change"
    data: ProfileChangePayload


# =====================================================================
# 11. PHASE 7.9.A.5.1 — EPHEMERAL AUTH HANDSHAKE
# =====================================================================


class AuthEvent(BaseModel):
    """First message sent by the client after WS connect — consumed by connect() before the loop."""
    event_type: Literal["auth"] = "auth"
    token: str


# =====================================================================
# 12. PHASE 7.9.B.12 — ANALYST PANE BRIDGE + PIPELINE PROGRESS
# =====================================================================


class AnalystQueryPayload(BaseModel):
    """Client → server: a message directed at the Natt analyst pane."""
    text: str
    session_id: Optional[str] = None
    context_paths: list[str] = Field(default_factory=list)
    # Phase 7.10.3 (ADR-703 G4) — caret offset for cursor-targeted semantic slicing.
    # Additive + optional: pre-7.11 clients omit it and the slice degrades gracefully.
    cursor: Optional[int] = None


class ClientAnalystQueryEvent(BaseModel):
    event_type: Literal["client_analyst_query"] = "client_analyst_query"
    data: AnalystQueryPayload


class NattMessagePayload(BaseModel):
    """Server → client: an analyst reply rendered in the Natt canvas."""
    content: str
    is_alert: bool = False


class ServerNattMessageEvent(BaseModel):
    event_type: Literal["server_natt_message"] = "server_natt_message"
    data: NattMessagePayload


# Phase 7.10.3 (ADR-703 + ADR-702) — token-by-token analyst streaming to the Natt pane.
class NattTokenChunkPayload(BaseModel):
    """Server → client: a single batched analyst token chunk for the Natt canvas."""
    token: str


class ServerNattTokenChunkEvent(BaseModel):
    event_type: Literal["server_natt_token"] = "server_natt_token"
    data: NattTokenChunkPayload


class NattStreamEndPayload(BaseModel):
    """Server → client: finalizes the streamed analyst bubble.

    Carries the G2 context version tag (a quick hash of the assembled context) so the
    7.11 extension can apply context-tolerant divergence if the buffer changed mid-stream.
    """
    context_version: Optional[str] = None


class ServerNattStreamEndEvent(BaseModel):
    event_type: Literal["server_natt_stream_end"] = "server_natt_stream_end"
    data: NattStreamEndPayload = Field(default_factory=NattStreamEndPayload)


class PipelineStepPayload(BaseModel):
    """Server → client: a single LangGraph node completed (progress, NOT chat)."""
    node_name: str
    status: Literal["completed"] = "completed"
    step_id: Optional[int] = None


class ServerPipelineStepEvent(BaseModel):
    event_type: Literal["server_pipeline_step"] = "server_pipeline_step"
    data: PipelineStepPayload


class ServerStreamEndEvent(BaseModel):
    """Server → client: the assistant message stream is finalized."""
    event_type: Literal["server_stream_end"] = "server_stream_end"
    data: dict = Field(default_factory=dict)


class ClientClearConversationEvent(BaseModel):
    """Client → server: drop the session's short-term chat memory (Phase 7.9.B.15)."""
    event_type: Literal["client_clear_conversation"] = "client_clear_conversation"
    data: dict = Field(default_factory=dict)


class ChatTurn(BaseModel):
    """One persisted chat turn (role + content) used to rehydrate session memory."""
    role: str
    content: str


class RestoreHistoryPayload(BaseModel):
    """Client → server: re-seed short-term chat memory from a persisted transcript."""
    messages: list[ChatTurn] = Field(default_factory=list)


class ClientRestoreHistoryEvent(BaseModel):
    """Client → server: rehydrate a reopened session's memory for continuity (Phase 7.9.B.20)."""
    event_type: Literal["client_restore_history"] = "client_restore_history"
    data: RestoreHistoryPayload


# =====================================================================
# 13. PHASE 7.11.1 — INLINE EDITOR MUTATIONS (ADR-706 §4.5a, Cmd+K)
# =====================================================================
#
# Two-layer transport (plan):
#   • Streaming intermediate deltas use the lightweight typed shape below
#     (the InlineMutationManager replays them into the active editor).
#   • The FINAL commit on user-accept reuses ApplyWorkspaceEditPayload (§9b)
#     so the SHA-256 stale-guard / OCC contract from 7.9.B.18 is preserved.
#
# All `offset` / `length` values are absolute character offsets into the
# LF-normalized file content (the frontend normalizes CRLF→LF before
# computing offsets, then converts each LF offset back to native editor
# coordinates before applying — see plan §W1).


class ClientInlineEditRequestPayload(BaseModel):
    """Client → server: a Cmd+K inline-edit request for a selected region."""

    edit_id: str                         # uuid4 hex — unique per request
    session_id: str
    file_path: str
    range_start: int                     # absolute LF-space offset of the selection start
    range_end: int                       # absolute LF-space offset of the selection end
    prompt: str                          # user's instruction (from showInputBox)
    base_hash: str                       # sha256(pre-edit, LF-normalized) for the final commit stale-guard
    selected_text: str                   # LF-normalized selection text (analyst-context provenance)
    language_id: Optional[str] = None    # VS Code languageId (None = unknown → validator passes through)


class ClientInlineEditRequestEvent(BaseModel):
    event_type: Literal["client_inline_edit_request"] = "client_inline_edit_request"
    data: ClientInlineEditRequestPayload


class ClientInlineEditCancelPayload(BaseModel):
    """Client → server: cancel an in-flight inline edit (user pressed Esc)."""

    edit_id: str
    session_id: str


class ClientInlineEditCancelEvent(BaseModel):
    event_type: Literal["client_inline_edit_cancel"] = "client_inline_edit_cancel"
    data: ClientInlineEditCancelPayload


class InlineEditStartPayload(BaseModel):
    """Server → client: stream is about to begin for edit_id (decorations now)."""

    edit_id: str
    session_id: str
    file_path: str
    range_start: int
    range_end: int


class ServerInlineEditStartEvent(BaseModel):
    event_type: Literal["server_inline_edit_start"] = "server_inline_edit_start"
    data: InlineEditStartPayload


class InlineEditDeltaPayload(BaseModel):
    """Server → client: one typed mutation delta to replay into the editor."""

    edit_id: str
    session_id: str
    kind: Literal["INSERT", "DELETE", "ABORT"]
    offset: int                          # absolute LF-space offset in the file
    length: int = 0                      # DELETE only — chars to remove from `offset`
    text: str = ""                       # INSERT body, or ABORT reason


class ServerInlineEditDeltaEvent(BaseModel):
    event_type: Literal["server_inline_edit_delta"] = "server_inline_edit_delta"
    data: InlineEditDeltaPayload


class InlineEditEndPayload(BaseModel):
    """Server → client: stream finalized (success or abort)."""

    edit_id: str
    session_id: str
    success: bool
    final_content: str = ""              # full post-edit LF-normalized content (used to recompute commit base_hash)
    error: Optional[str] = None


class ServerInlineEditEndEvent(BaseModel):
    event_type: Literal["server_inline_edit_end"] = "server_inline_edit_end"
    data: InlineEditEndPayload


# =====================================================================
# 14. PHASE 7.11.3 — ABORT CONTROLLER MESH (ADR-706 §4.5b, Stop button)
# =====================================================================
#
# Priority WS signal: the frontend asks the backend to cancel the in-flight
# generation task for `session_id`. The server resolves session_id → asyncio.Task
# via TaskService._active_tasks and calls task.cancel(); cooperative teardown
# (try/except CancelledError inside the runner) records partial FinOps, sets
# state["termination_reason"] = "user_abort", broadcasts a "Stopped by user"
# chat turn, and finalizes the stream via server_stream_end.


class ClientAbortMeshPayload(BaseModel):
    """Client → server: priority abort signal for an in-flight session task."""

    session_id: str


class ClientAbortMeshEvent(BaseModel):
    event_type: Literal["client_abort_mesh"] = "client_abort_mesh"
    data: ClientAbortMeshPayload


# =====================================================================
# 15. Phase 7.11.6 — Rich Tool Chips (ADR-706 §4.5f)
# =====================================================================
# Stateful tool-execution artifacts: every tracked tool invocation broadcasts
# a (server_tool_start, server_tool_stream_chunk*, server_tool_result) sequence
# so the frontend renders a chip with ANSI-decoded output, status badge, and
# Retry button. An optional server_tool_dep_graph follows when the tool's
# context includes dependency-graph data (sourced from
# core.memory.graphrag_extractor's dependency_graph SQLite table).
#
# Retry is "exact replay" — TaskService keeps the original ToolCallSpec in a
# session-keyed in-memory registry; client_retry_tool resolves
# (session_id, tool_call_id) → spec and re-invokes verbatim. Tools with
# `side_effect_free=False` (default) require a confirmation toast on the
# frontend BEFORE the Retry click fires the event.
#
# client_invoke_tracked_bash is a developer smoke command (palette-only): runs
# a one-shot sandbox_bash through the tracked path so the wire can be proven
# end-to-end without an agent-loop rewrite. Production tool flows will plug in
# the same `execute_tracked_tool` API from a future MCP/agent integration.


class ToolStartPayload(BaseModel):
    """Server → IDE: a tool execution just started; render a pending chip."""

    session_id: str
    tool_call_id: str
    tool_name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    side_effect_free: bool = False
    invoked_at: float  # unix timestamp, used by the frontend for relative-time display


class ServerToolStartEvent(BaseModel):
    event_type: Literal["server_tool_start"] = "server_tool_start"
    data: ToolStartPayload


class ToolStreamChunkPayload(BaseModel):
    """Server → IDE: incremental stdout/stderr chunk from a tracked tool.

    For Phase 7.11.6 the sandbox adapter is one-shot (`adapter.execute`
    returns a complete SandboxResult), so the backend emits exactly one chunk
    with the truncated body. Future streaming adapters can emit many.
    """

    session_id: str
    tool_call_id: str
    chunk: str
    is_stderr: bool = False


class ServerToolStreamChunkEvent(BaseModel):
    event_type: Literal["server_tool_stream_chunk"] = "server_tool_stream_chunk"
    data: ToolStreamChunkPayload


class ToolResultPayload(BaseModel):
    """Server → IDE: a tracked tool finished (success or error).

    `status="error"` covers both non-zero exit codes and exceptions inside
    the runner; `exit_code` is None when the failure was a Python exception
    rather than a process exit.
    """

    session_id: str
    tool_call_id: str
    status: Literal["success", "error"]
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None


class ServerToolResultEvent(BaseModel):
    event_type: Literal["server_tool_result"] = "server_tool_result"
    data: ToolResultPayload


class ToolDepGraphPayload(BaseModel):
    """Server → IDE: optional dependency-graph attachment for a tool result.

    Nodes carry `{id, label}`; edges carry `{from, to}`. Sourced from the
    GraphRAG `dependency_graph` SQLite table when a tool's context includes
    the file's k-hop neighborhood. The frontend renders this as a CSS/SVG
    disclosure tree (no d3 / no canvas).
    """

    session_id: str
    tool_call_id: str
    nodes: List[Dict[str, str]] = Field(default_factory=list)
    edges: List[Dict[str, str]] = Field(default_factory=list)


class ServerToolDepGraphEvent(BaseModel):
    event_type: Literal["server_tool_dep_graph"] = "server_tool_dep_graph"
    data: ToolDepGraphPayload


class ClientRetryToolPayload(BaseModel):
    """IDE → server: exact-replay request for a previously tracked tool call.

    The backend looks up `(session_id, tool_call_id)` in TaskService's
    `_tool_call_registry`; an unknown id is a no-op (logged at INFO).
    """

    session_id: str
    tool_call_id: str


class ClientRetryToolEvent(BaseModel):
    event_type: Literal["client_retry_tool"] = "client_retry_tool"
    data: ClientRetryToolPayload


class ClientInvokeTrackedBashPayload(BaseModel):
    """IDE → server: developer smoke command (palette `/dev/run-bash <cmd>`).

    Routes through the same `execute_tracked_tool("sandbox_bash", ...)` path
    that future MCP/agent flows will use, so the chip pipeline is provably
    live end-to-end without depending on agent refactors.
    """

    session_id: str
    command: str
    timeout_sec: float = 30.0
    working_dir: Optional[str] = None


class ClientInvokeTrackedBashEvent(BaseModel):
    event_type: Literal["client_invoke_tracked_bash"] = "client_invoke_tracked_bash"
    data: ClientInvokeTrackedBashPayload


# =====================================================================
# 16. Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel Debugging (Thread Branching)
# =====================================================================
# Three events + one additive field on TaskPayload (in core/task_service.py).
# Schema-additive only: pre-7.11.8 clients/servers that don't know these
# events keep working.

class ClientBranchFromCheckpointPayload(BaseModel):
    """IDE → server: fork a session from a specific historical checkpoint.

    The backend mints a fresh ``new_session_id`` (UUID4 hex), copies the
    ``(parent_session_id, from_checkpoint_id)`` row in ``hybrid_checkpoints``
    into the new thread (parent_id linkage preserved for lineage walks), and
    responds with ``server_session_branched``. The frontend then opens the
    new session in the sidebar, seeded with the parent's transcript up to
    that point.
    """

    parent_session_id: str
    from_checkpoint_id: str


class ClientBranchFromCheckpointEvent(BaseModel):
    event_type: Literal["client_branch_from_checkpoint"] = "client_branch_from_checkpoint"
    data: ClientBranchFromCheckpointPayload


class ServerSessionBranchedPayload(BaseModel):
    """Server → client: a new session has been minted from a branch operation.

    The frontend uses ``parent_session_id`` + ``from_checkpoint_id`` to slice
    the parent's persisted transcript (purely cosmetic; the backend's L2 row
    is the canonical graph-state source) and seeds the new session's UI
    history. ``new_session_id`` is the UUID4 the host should use for
    ``SessionManager.createSession``.
    """

    parent_session_id: str
    new_session_id: str
    from_checkpoint_id: str


class ServerSessionBranchedEvent(BaseModel):
    event_type: Literal["server_session_branched"] = "server_session_branched"
    data: ServerSessionBranchedPayload


# =====================================================================
# 4. EL CONTRATO MAESTRO O(1)
# =====================================================================

# FastAPI usará este tipo para validar CUALQUIER mensaje entrante.
# Pydantic usará el campo 'event_type' para castearlo a la clase correcta.

WebSocketMessage = Union[
    ClientFileUpdateEvent,
    ServerCodeProposalEvent,
    ServerStatusEvent,
    ServerTokenChunkEvent,
    ServerThinkingChunkEvent,         # Phase 9 — Native Thinking reasoning delta
    ServerTelemetryEvent,
    ServerGraphMutationEvent,
    ServerHITLApprovalRequestEvent,
    ClientPlannerModeToggleEvent,
    ClientHITLResponseEvent,
    ClientConcurrencyConflictEvent,
    ServerModelWarmupEvent,
    ClientWorkspaceInitEvent,        # Phase 2.5
    ServerIndexingProgressEvent,     # Phase 2.5
    ServerIndexingErrorEvent,        # Phase 2.5 — pre-flight error
    ServerByomConfigAppliedEvent,    # Phase 7.9.B.11 — preset applied notification
    ClientFileDeleteEvent,           # Phase 2.1.13
    ServerVfsPatchApprovedEvent,     # Phase 2.22.4
    ServerApplyWorkspaceEditEvent,   # Phase 7.9.B.18 — write pipeline dispatch
    ClientPatchAppliedEvent,         # Phase 7.9.B.18 — write pipeline ack
    ClientMasterToggleEvent,         # Phase 3.4.1
    ClientProfileChangeEvent,        # Phase 3.4.1
    AuthEvent,                       # Phase 7.9.A.5.1 — ephemeral auth handshake
    ClientAnalystQueryEvent,         # Phase 7.9.B.12 — Natt analyst pane query
    ServerNattMessageEvent,          # Phase 7.9.B.12 — analyst reply
    ServerNattTokenChunkEvent,       # Phase 7.10.3 — streamed analyst token chunk
    ServerNattStreamEndEvent,        # Phase 7.10.3 — analyst stream finalized (+ context_version)
    ServerPipelineStepEvent,         # Phase 7.9.B.12 — pipeline node progress
    ServerStreamEndEvent,            # Phase 7.9.B.12 — assistant stream finalized
    ClientClearConversationEvent,    # Phase 7.9.B.15 — clear short-term chat memory
    ClientRestoreHistoryEvent,       # Phase 7.9.B.20 — rehydrate session memory on reopen
    ClientInlineEditRequestEvent,    # Phase 7.11.1 — Cmd+K inline edit request
    ClientInlineEditCancelEvent,     # Phase 7.11.1 — inline edit cancel
    ServerInlineEditStartEvent,      # Phase 7.11.1 — stream start (open decorations)
    ServerInlineEditDeltaEvent,      # Phase 7.11.1 — typed mutation delta
    ServerInlineEditEndEvent,        # Phase 7.11.1 — stream finalized
    ClientAbortMeshEvent,            # Phase 7.11.3 — abort controller mesh (Stop button)
    ServerToolStartEvent,            # Phase 7.11.6 — Rich Tool Chips: tool started
    ServerToolStreamChunkEvent,      # Phase 7.11.6 — Rich Tool Chips: incremental output
    ServerToolResultEvent,           # Phase 7.11.6 — Rich Tool Chips: tool finished
    ServerToolDepGraphEvent,         # Phase 7.11.6 — Rich Tool Chips: optional dep-graph attachment
    ClientRetryToolEvent,            # Phase 7.11.6 — Rich Tool Chips: exact-replay retry
    ClientInvokeTrackedBashEvent,    # Phase 7.11.6 — Rich Tool Chips: dev smoke command
    ClientBranchFromCheckpointEvent, # Phase 7.11.8 — time-travel: fork from a checkpoint
    ServerSessionBranchedEvent,      # Phase 7.11.8 — time-travel: new session minted from fork
]
