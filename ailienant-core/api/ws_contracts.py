# alienant-core/core/ws_contracts.py

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional, Union

# =====================================================================
# 1. EVENT PAYLOADS
# =====================================================================


class FileUpdatePayload(BaseModel):
    """Payload for when the IDE sends fresh code to the Backend."""

    filepath: str = Field(..., description="Absolute path of the file in the IDE")
    content: str = Field(..., description="Current content in the VS Code buffer")
    # This is the heart of the OCC that we defined in state.py
    document_version_id: str = Field(..., description="Timestamp o Hash del IDE")


class CodeProposalPayload(BaseModel):
    """Payload for when the AI ​​wants to write to the IDE."""

    filepath: str
    proposed_content: str
    # The AI ​​sends the ID on which it based its calculations
    base_document_version_id: str
    agent_signature: str = Field(..., description="Proposing Agent (e.g. LogicAgent)")


class StatusPayload(BaseModel):
    """Ephemeral payload to update the extension's UI."""

    agent_name: str
    status_message: str
    is_error: bool = False


# =====================================================================
# 2. EVENT WRAPPERS (Tagged Unions)
# =====================================================================


class ClientFileUpdateEvent(BaseModel):
    # The 'event_type' key is our constant discriminator.
    event_type: Literal["client_file_update"] = "client_file_update"
    data: FileUpdatePayload


class ServerCodeProposalEvent(BaseModel):
    event_type: Literal["server_code_proposal"] = "server_code_proposal"
    data: CodeProposalPayload


class ServerStatusEvent(BaseModel):
    event_type: Literal["server_status"] = "server_status"
    data: StatusPayload


# =====================================================================
# 3. BIDIRECTIONAL STREAMING & HITL CONTRACTS
# =====================================================================


class TokenChunkPayload(BaseModel):
    """Single LLM output token streamed to the IDE."""

    token: str
    step_id: Optional[int] = None   # WBS step currently executing, if known


class ThinkingChunkPayload(BaseModel):
    """ a native-reasoning ("thinking") delta streamed to the
    IDE's collapsible Thought Box.

    Distinct from ``TokenChunkPayload`` (the answer stream) and from
    ``PipelineStepPayload`` (synthesized node narration). ``delta`` is
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
    # Optional human-readable note when the router degraded for hardware reasons
    # (VRAM floor / predicted context overflow), so the IDE can surface the cause
    # of a slowdown. Additive and tolerant of absence for older clients.
    routing_warning: Optional[str] = None


class GraphMutationPayload(BaseModel):
    """WBS step status transition emitted by the Orchestrator."""

    step_number: int
    new_status: Literal["pending", "in_progress", "completed", "failed"]
    agent_name: Optional[str] = None


class PlannerModeTogglePayload(BaseModel):
    """Client request to switch Planner-only mode on or off."""

    active: bool


class ProposedFile(BaseModel):
    """One file's proposed post-edit state, ridden inside a FILE_WRITE approval.

    Carried in the approval request itself (not a separate broadcast) so the
    diff and the authorization can never desync on the client: a dropped or
    late preview can no longer leave the Accept/Reject row without a diff.
    """

    file_path: str
    new_content: str
    base_hash: Optional[str] = None


class HITLApprovalRequestPayload(BaseModel):
    """Backend suspends and asks the human to approve a proposed action."""

    session_id: str
    approval_id: str                    # UUID4 — unique per request; client must echo this back
    action_description: str
    proposed_content: Optional[str] = None
    # FILE_WRITE only: the proposed post-edit content per file, so the host can
    # render the inline diff in-chat before apply. None for non-write kinds.
    proposed_files: Optional[List[ProposedFile]] = None
    # additive classifier so the native-toast
    # surface can choose severity (info vs warning) and emit a short title.
    # Backward-compatible: pre - payloads omit this field and pydantic
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
    # optional edited payload from the HITL card's edit mode.
    # For a single-file patch, this overrides the proposed content before apply.
    modified_content: Optional[str] = None


# --- Server → Client Events ---

class ServerTokenChunkEvent(BaseModel):
    event_type: Literal["server_token_chunk"] = "server_token_chunk"
    data: TokenChunkPayload


class ServerThinkingChunkEvent(BaseModel):
    # Native Thinking reasoning delta.
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


class RegisterSessionPayload(BaseModel):
    """One panel announcing its session id on the shared connection."""

    session_id: str


class ClientRegisterSessionEvent(BaseModel):
    """Multiplexing handshake: the client announces a session id so the backend
    aliases it onto this physical socket. One socket serves many sessions; the
    client re-sends this for every active session on each (re)connect."""

    event_type: Literal["client_register_session"] = "client_register_session"
    data: RegisterSessionPayload


# =====================================================================
# 5. OCC — OPTIMISTIC CONCURRENCY CONTROL
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
# 6. MODEL WARMUP SIGNAL
# =====================================================================


class ModelWarmupPayload(BaseModel):
    """Emitted before a local model inference call that may require warmup time."""

    model_name: str
    is_local: bool
    tier: Literal["small", "medium", "big"]


class ServerModelWarmupEvent(BaseModel):
    event_type: Literal["server_model_warmup"] = "server_model_warmup"
    data: ModelWarmupPayload


class OomEngagedPayload(BaseModel):
    """Surfaces an OOM rescue swap so the IDE can warn the user that the local
    model ran out of memory/context and the turn was re-emitted to the cloud."""

    failed_model: str
    fallback_model: str


class ServerOomEngagedEvent(BaseModel):
    event_type: Literal["server_oom_engaged"] = "server_oom_engaged"
    data: OomEngagedPayload


# =====================================================================
# 7. LAZY WORKSPACE INDEXING CONTRACTS
# =====================================================================


class WorkspaceInitPayload(BaseModel):
    """Client announces the workspace root path for lazy background indexing."""
    workspace_root: str
    project_id: str
    workspace_pid: Optional[int] = None  # VS Code window PID


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
# 8. FILE DELETE / UNLINK EVENTS
# =====================================================================


class FileDeletePayload(BaseModel):
    """Payload for IDE file deletion (unlink) events."""
    filepath: str = Field(..., description="Absolute path of the deleted file")
    project_id: str = Field(default="", description="Project scope for DB purge")


class ClientFileDeleteEvent(BaseModel):
    event_type: Literal["client_file_delete"] = "client_file_delete"
    data: FileDeletePayload


# =====================================================================
# 8b. IDE TELEMETRY BUS — SILENT FILE-LIFECYCLE CHANNEL
# =====================================================================
# Push-model spine: file lifecycle (save / create / rename) travels on a
# dedicated silent channel over the existing socket — never a toast, never an
# interruption of the chat/answer stream. Metadata only: the content source is
# the RAM-VFS buffer (kept hot by ClientFileUpdateEvent) or disk, so the bus
# stays lightweight and droppable. Deletes are NOT folded here — they keep the
# purpose-built ClientFileDeleteEvent (graph purge) contract.


class IdeTelemetryPayload(BaseModel):
    """Silent IDE lifecycle signal feeding the reactive index and Push panels.

    ``old_path`` is set only for renames so the consumer can migrate a graph
    node in place rather than purge-and-reindex. ``document_version_id`` mirrors
    the OCC token carried by ClientFileUpdateEvent for save coalescing.
    """

    action: Literal["file_saved", "file_created", "file_renamed"]
    filepath: str = Field(..., description="Absolute path of the affected file (new path on rename)")
    old_path: Optional[str] = Field(default=None, description="Previous path; set only on file_renamed")
    document_version_id: str = ""


class ClientIdeTelemetryEvent(BaseModel):
    event_type: Literal["client_ide_telemetry"] = "client_ide_telemetry"
    data: IdeTelemetryPayload


# =====================================================================
# 8c. MANUAL DREAMING — EXPLICIT "CONSOLIDATE MEMORY" TRIGGER
# =====================================================================
# Memory consolidation never wakes on a timer; it fires only on an explicit
# user action (HUD button / VS Code command). ``focus_area`` lets the operator
# scope the pass to a theme (saving consolidation tokens); ``None`` consolidates
# the whole workspace ("Auto").


class DreamingRunPayload(BaseModel):
    """Client → server: run one manual memory-consolidation pass."""

    focus_area: Optional[str] = Field(
        default=None,
        description="Theme to prioritize; None = Auto (whole workspace)",
    )


class ClientDreamingRunEvent(BaseModel):
    event_type: Literal["client_dreaming_run"] = "client_dreaming_run"
    data: DreamingRunPayload


# =====================================================================
# 9. VFS PATCH APPROVED (IPC Bridge)
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
# 9b. ENTERPRISE WRITE PIPELINE (VS Code applyEdit bridge)
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
# 10. INTELLIGENCE PROFILE / MASTER TOGGLE EVENTS
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
# 11. EPHEMERAL AUTH HANDSHAKE
# =====================================================================


class AuthEvent(BaseModel):
    """First message sent by the client after WS connect — consumed by connect() before the loop."""
    event_type: Literal["auth"] = "auth"
    token: str


# =====================================================================
# 12. ANALYST PANE BRIDGE + PIPELINE PROGRESS
# =====================================================================


class AnalystQueryPayload(BaseModel):
    """Client → server: a message directed at the Natt analyst pane."""
    text: str
    session_id: Optional[str] = None
    context_paths: list[str] = Field(default_factory=list)
    # caret offset for cursor-targeted semantic slicing.
    # Additive + optional: pre - clients omit it and the slice degrades gracefully.
    cursor: Optional[int] = None
    # Answer-model tier picked in the Natt HUD (small | medium | big | cloud).
    # Additive + optional: pre-clients omit it and the analyst defaults to medium.
    # Only changes generation; retrieval + embeddings are unaffected.
    model_tier: Optional[str] = None


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


# token-by-token analyst streaming to the Natt pane.
class NattTokenChunkPayload(BaseModel):
    """Server → client: a single batched analyst token chunk for the Natt canvas."""
    token: str


class ServerNattTokenChunkEvent(BaseModel):
    event_type: Literal["server_natt_token"] = "server_natt_token"
    data: NattTokenChunkPayload


class NattStreamEndPayload(BaseModel):
    """Server → client: finalizes the streamed analyst bubble.

    Carries the G2 context version tag (a quick hash of the assembled context) so the
    extension can apply context-tolerant divergence if the buffer changed mid-stream.
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


class PlanDocumentPayload(BaseModel):
    """Server → client: the finalized plan as structured data for the rich Plan
    surface (NOT chat prose). Mirrors MissionSpecification's public shape.

    ``summary`` is the one-line chat-bubble pointer; it travels WITH the structure
    so the conversation text and the docked panel land in a single frontend state
    transition — two sequential broadcasts could otherwise arrive out of order and
    flash the pointer against an empty panel.
    """
    summary: str
    outcome: str
    scope: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)  # serialized WBSStep rows
    checks: List[str] = Field(default_factory=list)
    ubiquitous_language: Dict[str, str] = Field(default_factory=dict)


class ServerPlanDocumentEvent(BaseModel):
    """Server → client: a finalized plan, structured for the rich Plan surface."""
    event_type: Literal["server_plan_document"] = "server_plan_document"
    data: PlanDocumentPayload


class ServerStreamEndEvent(BaseModel):
    """Server → client: the assistant message stream is finalized."""
    event_type: Literal["server_stream_end"] = "server_stream_end"
    data: Dict[str, Any] = Field(default_factory=dict)


class ClientClearConversationEvent(BaseModel):
    """Client → server: drop the session's short-term chat memory."""
    event_type: Literal["client_clear_conversation"] = "client_clear_conversation"
    data: Dict[str, Any] = Field(default_factory=dict)


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
# 13. INLINE EDITOR MUTATIONS (Cmd+K)
# =====================================================================
#
# Two-layer transport (plan):
#   • Streaming intermediate deltas use the lightweight typed shape below
#     (the InlineMutationManager replays them into the active editor).
#   • The FINAL commit on user-accept reuses ApplyWorkspaceEditPayload
#     so the SHA-256 stale-guard / OCC contract is preserved.
#
# All `offset` / `length` values are absolute character offsets into the
# LF-normalized file content (the frontend normalizes CRLF→LF before
# computing offsets, then converts each LF offset back to native editor
# coordinates before applying).


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
# 14. ABORT CONTROLLER MESH (Stop button)
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


class ClientPtyWritePayload(BaseModel):
    """Client → server: a line of stdin for a session's live interactive terminal.

    Routes to the persistent sandbox session keyed by ``session_id`` so the user can
    answer a blocking prompt (e.g. ``[Y/n]``). ``data`` is the raw text to feed,
    including any trailing newline the prompt expects.
    """

    session_id: str
    data: str


class ClientPtyWriteEvent(BaseModel):
    event_type: Literal["client_pty_write"] = "client_pty_write"
    data: ClientPtyWritePayload


# --- Delivery acknowledgements (server → client) ---
# A Stop with the socket down, or a HITL response from a torn-down webview, must
# never be a silent fire-and-forget. The server echoes a terse ACK the instant it
# has acted so the UI can confirm (or surface a failure) instead of freezing.


class AbortAckPayload(BaseModel):
    """Server → client: result of resolving a client_abort_mesh request.

    ``signalled`` is True when a live in-flight task was found and cancelled,
    False when no registered task existed (already finished / never started).
    """

    session_id: str
    signalled: bool


class ServerAbortAckEvent(BaseModel):
    event_type: Literal["server_abort_ack"] = "server_abort_ack"
    data: AbortAckPayload


class HitlAckPayload(BaseModel):
    """Server → client: confirmation that a client_hitl_response was applied."""

    approval_id: str
    ok: bool


class ServerHitlAckEvent(BaseModel):
    event_type: Literal["server_hitl_ack"] = "server_hitl_ack"
    data: HitlAckPayload


# =====================================================================
# 15. Rich Tool Chips
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

    For the sandbox adapter is one-shot (`adapter.execute`
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
# 16. Time-Travel Debugging (Thread Branching)
# =====================================================================
# Three events + one additive field on TaskPayload (in core/task_service.py).
# Schema-additive only: clients/servers that don't know these
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
# 17. Agentic Cell Glass-Box Telemetry Events
# =====================================================================

class CellToolStartPayload(BaseModel):
    session_id: str
    iteration: int
    tool_name: str
    args_scrubbed: Dict[str, str]


class ServerCellToolStartEvent(BaseModel):
    event_type: Literal["server_cell_tool_start"] = "server_cell_tool_start"
    data: CellToolStartPayload


class CellPtyChunkPayload(BaseModel):
    session_id: str
    iteration: int
    text: str
    is_stderr: bool = False


class ServerCellPtyChunkEvent(BaseModel):
    event_type: Literal["server_cell_pty_chunk"] = "server_cell_pty_chunk"
    data: CellPtyChunkPayload


class CellAstDiffPayload(BaseModel):
    session_id: str
    iteration: int
    path: str
    search: str
    replace: str


class ServerCellAstDiffEvent(BaseModel):
    event_type: Literal["server_cell_ast_diff"] = "server_cell_ast_diff"
    data: CellAstDiffPayload


class CellGovernorTickPayload(BaseModel):
    session_id: str
    step: int
    cost_usd: float
    elapsed_s: float
    axis: Optional[str] = None


class ServerCellGovernorTickEvent(BaseModel):
    event_type: Literal["server_cell_governor_tick"] = "server_cell_governor_tick"
    data: CellGovernorTickPayload


# =====================================================================
# 18. THE MASTER CONTRACT O(1)
# =====================================================================

# FastAPI will use this type to validate ANY incoming message.
# Pydantic will use the 'event_type' field to cast it to the correct class.

WebSocketMessage = Union[
    ClientFileUpdateEvent,
    ServerCodeProposalEvent,
    ServerStatusEvent,
    ServerTokenChunkEvent,
    ServerThinkingChunkEvent,         # Native Thinking reasoning delta
    ServerTelemetryEvent,
    ServerGraphMutationEvent,
    ServerHITLApprovalRequestEvent,
    ClientPlannerModeToggleEvent,
    ClientHITLResponseEvent,
    ClientRegisterSessionEvent,      # multiplexing handshake — alias session→socket
    ClientConcurrencyConflictEvent,
    ServerModelWarmupEvent,
    ServerOomEngagedEvent,           # OOM rescue swap surfaced to the IDE
    ClientWorkspaceInitEvent,        
    ServerIndexingProgressEvent,     
    ServerIndexingErrorEvent,        # pre-flight error
    ServerByomConfigAppliedEvent,    # preset applied notification
    ClientFileDeleteEvent,           
    ClientIdeTelemetryEvent,         # IDE telemetry bus — silent file-lifecycle channel
    ClientDreamingRunEvent,          # Manual Dreaming — explicit consolidate-memory trigger
    ServerVfsPatchApprovedEvent,     
    ServerApplyWorkspaceEditEvent,   # write pipeline dispatch
    ClientPatchAppliedEvent,         # write pipeline ack
    ClientMasterToggleEvent,         
    ClientProfileChangeEvent,        
    AuthEvent,                       # ephemeral auth handshake
    ClientAnalystQueryEvent,         # Natt analyst pane query
    ServerNattMessageEvent,          # analyst reply
    ServerNattTokenChunkEvent,       # streamed analyst token chunk
    ServerNattStreamEndEvent,        # analyst stream finalized (+ context_version)
    ServerPipelineStepEvent,         # pipeline node progress
    ServerPlanDocumentEvent,         # finalized plan → rich Plan surface
    ServerStreamEndEvent,            # assistant stream finalized
    ClientClearConversationEvent,    # clear short-term chat memory
    ClientRestoreHistoryEvent,       # rehydrate session memory on reopen
    ClientInlineEditRequestEvent,    # Cmd+K inline edit request
    ClientInlineEditCancelEvent,     # inline edit cancel
    ServerInlineEditStartEvent,      # stream start (open decorations)
    ServerInlineEditDeltaEvent,      # typed mutation delta
    ServerInlineEditEndEvent,        # stream finalized
    ClientAbortMeshEvent,            # abort controller mesh (Stop button)
    ClientPtyWriteEvent,             # interactive terminal: stdin line into the live session
    ServerAbortAckEvent,             # abort delivery acknowledgement
    ServerHitlAckEvent,              # HITL response delivery acknowledgement
    ServerToolStartEvent,            # Rich Tool Chips: tool started
    ServerToolStreamChunkEvent,      # Rich Tool Chips: incremental output
    ServerToolResultEvent,           # Rich Tool Chips: tool finished
    ServerToolDepGraphEvent,         # Rich Tool Chips: optional dep-graph attachment
    ClientRetryToolEvent,            # Rich Tool Chips: exact-replay retry
    ClientInvokeTrackedBashEvent,    # Rich Tool Chips: dev smoke command
    ClientBranchFromCheckpointEvent, # time-travel: fork from a checkpoint
    ServerSessionBranchedEvent,      # time-travel: new session minted from fork
    ServerCellToolStartEvent,        # cell glass-box: tool call started
    ServerCellPtyChunkEvent,         # cell glass-box: PTY output chunk (streaming)
    ServerCellAstDiffEvent,          # cell glass-box: AST mutation applied
    ServerCellGovernorTickEvent,     # cell glass-box: budget governor tick
]
