// ============================================================
// AILIENANT — WebSocket wire contracts (TypeScript mirror)
//
// Typed discriminated unions for every server↔client WebSocket event, keyed on
// the `event_type` discriminant. This is the TypeScript mirror of the backend's
// authoritative `WebSocketMessage` union in `ailienant-core/api/ws_contracts.py`;
// the `event_type` string literals are kept byte-identical to the Pydantic
// `Literal[...]` tags so a payload validated on one side narrows on the other.
//
// Two notes:
//   • Membership is split by message origin (server→client vs client→server),
//     NOT by the `event_type` string prefix — `state_compacted` is a SERVER event
//     that lacks the `server_` prefix.
//   • Forward-compatibility is structural: TypeScript tolerates unknown extra
//     fields, so a newer server that adds a field does not break an older client
//     (the wire contract is additive-only). Optional/nullable fields model the
//     fields a sender may omit or send as null.
//
// Pure type module — no runtime imports — so it is safe to import from both the
// extension host and webview bundles.
// ============================================================

// ── Shared sub-payload shapes ───────────────────────────────

/** One file's proposed post-edit state, ridden inside a FILE_WRITE approval. */
export interface ProposedFile {
    file_path: string;
    unified_diff?: string | null;
    /** Deprecated — reconstructed host-side from `unified_diff`; kept additive. */
    new_content?: string | null;
    base_hash?: string | null;
}

/** One file edit dispatched to the VS Code applyEdit actuator. */
export interface WorkspaceEditItem {
    file_path: string;
    new_content: string;
    base_hash?: string | null;
}

/** One persisted chat turn used to rehydrate session memory. */
export interface ChatTurn {
    role: string;
    content: string;
}

// ============================================================
// SERVER → CLIENT EVENTS (35)
// ============================================================

export interface TokenChunkPayload {
    token: string;
    step_id?: number | null;
}
export interface ServerTokenChunkEvent {
    event_type: 'server_token_chunk';
    data: TokenChunkPayload;
}

export interface ThinkingChunkPayload {
    session_id: string;
    delta: string;
    token_count: number;
}
export interface ServerThinkingChunkEvent {
    event_type: 'server_thinking_chunk';
    data: ThinkingChunkPayload;
}

export interface TelemetryPayload {
    session_id: string;
    routing_decision: string;
    css_total: number;
    task_complexity_index: number;
    is_red_alert: boolean;
    routing_warning?: string | null;
}
export interface ServerTelemetryEvent {
    event_type: 'server_telemetry';
    data: TelemetryPayload;
}

export interface GraphMutationPayload {
    step_number: number;
    new_status: 'pending' | 'in_progress' | 'completed' | 'failed';
    agent_name?: string | null;
}
export interface ServerGraphMutationEvent {
    event_type: 'server_graph_mutation';
    data: GraphMutationPayload;
}

export interface HITLApprovalRequestPayload {
    session_id: string;
    approval_id: string;
    action_description: string;
    proposed_content?: string | null;
    proposed_files?: ProposedFile[] | null;
    request_kind?: string | null;
    risk_patterns_matched?: string[] | null;
}
export interface ServerHITLApprovalRequestEvent {
    event_type: 'server_hitl_approval_request';
    data: HITLApprovalRequestPayload;
}

export interface ModelWarmupPayload {
    model_name: string;
    is_local: boolean;
    tier: 'small' | 'medium' | 'big';
}
export interface ServerModelWarmupEvent {
    event_type: 'server_model_warmup';
    data: ModelWarmupPayload;
}

export interface OomEngagedPayload {
    failed_model: string;
    fallback_model: string;
}
export interface ServerOomEngagedEvent {
    event_type: 'server_oom_engaged';
    data: OomEngagedPayload;
}

export interface IndexingProgressPayload {
    current: number;
    total: number;
    percentage: number;
}
export interface ServerIndexingProgressEvent {
    event_type: 'server_indexing_progress';
    data: IndexingProgressPayload;
}

export interface IndexingErrorPayload {
    reason: string;
}
export interface ServerIndexingErrorEvent {
    event_type: 'server_indexing_error';
    data: IndexingErrorPayload;
}

export interface ByomConfigAppliedPayload {
    preset_id: string;
    preset_name: string;
}
export interface ServerByomConfigAppliedEvent {
    event_type: 'server_byom_config_applied';
    data: ByomConfigAppliedPayload;
}

export interface VfsPatchApprovedPayload {
    file_path: string;
    unified_diff: string;
    mode: 'autonomous' | 'supervision';
}
export interface ServerVfsPatchApprovedEvent {
    event_type: 'server_vfs_patch_approved';
    data: VfsPatchApprovedPayload;
}

export interface ApplyWorkspaceEditPayload {
    patch_id: string;
    save: boolean;
    edits: WorkspaceEditItem[];
}
export interface ServerApplyWorkspaceEditEvent {
    event_type: 'server_apply_workspace_edit';
    data: ApplyWorkspaceEditPayload;
}

// ── Devcontainer host execution bridge (trusted tier) ────────────────────────
// The backend routes trusted provisioning + command execution over these events
// to the host, which owns the local container runtime. Every event is correlated
// by `request_id`; `env_keys` carries variable NAMES only (never values).

export interface DevcontainerProvisionRequestPayload {
    session_id: string;
    request_id: string;
    cwd: string;
}
export interface ServerDevcontainerProvisionRequestEvent {
    event_type: 'server_devcontainer_provision_request';
    data: DevcontainerProvisionRequestPayload;
}

export interface DevcontainerExecRequestPayload {
    session_id: string;
    request_id: string;
    command: string;
    cwd: string;
    env_keys: string[];
}
export interface ServerDevcontainerExecRequestEvent {
    event_type: 'server_devcontainer_exec_request';
    data: DevcontainerExecRequestPayload;
}

export interface DevcontainerProvisionStatusPayload {
    session_id: string;
    request_id: string;
    state: 'provisioning' | 'ready' | 'timeout' | 'failed';
}
export interface ClientDevcontainerProvisionStatusEvent {
    event_type: 'client_devcontainer_provision_status';
    data: DevcontainerProvisionStatusPayload;
}

export interface DevcontainerExecStreamPayload {
    session_id: string;
    request_id: string;
    stream: 'stdout' | 'stderr';
    chunk: string;
}
export interface ClientDevcontainerExecStreamEvent {
    event_type: 'client_devcontainer_exec_stream';
    data: DevcontainerExecStreamPayload;
}

export interface DevcontainerExecExitPayload {
    session_id: string;
    request_id: string;
    exit_code: number;
}
export interface ClientDevcontainerExecExitEvent {
    event_type: 'client_devcontainer_exec_exit';
    data: DevcontainerExecExitPayload;
}

export interface NattMessagePayload {
    content: string;
    is_alert: boolean;
}
export interface ServerNattMessageEvent {
    event_type: 'server_natt_message';
    data: NattMessagePayload;
}

export interface NattTokenChunkPayload {
    token: string;
}
export interface ServerNattTokenChunkEvent {
    event_type: 'server_natt_token';
    data: NattTokenChunkPayload;
}

export interface NattStreamEndPayload {
    context_version?: string | null;
}
export interface ServerNattStreamEndEvent {
    event_type: 'server_natt_stream_end';
    data: NattStreamEndPayload;
}

export interface PipelineStepPayload {
    node_name: string;
    status: 'completed';
    step_id?: number | null;
}
export interface ServerPipelineStepEvent {
    event_type: 'server_pipeline_step';
    data: PipelineStepPayload;
}

export interface PlanDocumentPayload {
    summary: string;
    outcome: string;
    scope: string[];
    constraints: string[];
    decisions: string[];
    /** Serialized WBSStep rows. */
    tasks: Array<Record<string, unknown>>;
    checks: string[];
    ubiquitous_language: Record<string, string>;
}
export interface ServerPlanDocumentEvent {
    event_type: 'server_plan_document';
    data: PlanDocumentPayload;
}

export interface ServerStreamEndEvent {
    event_type: 'server_stream_end';
    data: Record<string, unknown>;
}

export interface InlineEditStartPayload {
    edit_id: string;
    session_id: string;
    file_path: string;
    range_start: number;
    range_end: number;
}
export interface ServerInlineEditStartEvent {
    event_type: 'server_inline_edit_start';
    data: InlineEditStartPayload;
}

export interface InlineEditDeltaPayload {
    edit_id: string;
    session_id: string;
    kind: 'INSERT' | 'DELETE' | 'ABORT';
    offset: number;
    length: number;
    text: string;
}
export interface ServerInlineEditDeltaEvent {
    event_type: 'server_inline_edit_delta';
    data: InlineEditDeltaPayload;
}

export interface InlineEditEndPayload {
    edit_id: string;
    session_id: string;
    success: boolean;
    final_content: string;
    error?: string | null;
}
export interface ServerInlineEditEndEvent {
    event_type: 'server_inline_edit_end';
    data: InlineEditEndPayload;
}

export interface AbortAckPayload {
    session_id: string;
    signalled: boolean;
}
export interface ServerAbortAckEvent {
    event_type: 'server_abort_ack';
    data: AbortAckPayload;
}

export interface HitlAckPayload {
    approval_id: string;
    ok: boolean;
}
export interface ServerHitlAckEvent {
    event_type: 'server_hitl_ack';
    data: HitlAckPayload;
}

export interface ToolStartPayload {
    session_id: string;
    tool_call_id: string;
    tool_name: string;
    args: Record<string, unknown>;
    side_effect_free: boolean;
    /** Unix timestamp for relative-time display. */
    invoked_at: number;
}
export interface ServerToolStartEvent {
    event_type: 'server_tool_start';
    data: ToolStartPayload;
}

export interface ToolStreamChunkPayload {
    session_id: string;
    tool_call_id: string;
    chunk: string;
    is_stderr: boolean;
}
export interface ServerToolStreamChunkEvent {
    event_type: 'server_tool_stream_chunk';
    data: ToolStreamChunkPayload;
}

export interface ToolResultPayload {
    session_id: string;
    tool_call_id: string;
    status: 'success' | 'error';
    exit_code?: number | null;
    duration_ms?: number | null;
}
export interface ServerToolResultEvent {
    event_type: 'server_tool_result';
    data: ToolResultPayload;
}

export interface ToolDepGraphPayload {
    session_id: string;
    tool_call_id: string;
    nodes: Array<{ id: string; label: string }>;
    edges: Array<{ from: string; to: string }>;
}
export interface ServerToolDepGraphEvent {
    event_type: 'server_tool_dep_graph';
    data: ToolDepGraphPayload;
}

export interface ServerSessionBranchedPayload {
    parent_session_id: string;
    new_session_id: string;
    from_checkpoint_id: string;
}
export interface ServerSessionBranchedEvent {
    event_type: 'server_session_branched';
    data: ServerSessionBranchedPayload;
}

export interface CellToolStartPayload {
    session_id: string;
    iteration: number;
    tool_name: string;
    args_scrubbed: Record<string, string>;
}
export interface ServerCellToolStartEvent {
    event_type: 'server_cell_tool_start';
    data: CellToolStartPayload;
}

export interface CellPtyChunkPayload {
    session_id: string;
    iteration: number;
    text: string;
    is_stderr: boolean;
}
export interface ServerCellPtyChunkEvent {
    event_type: 'server_cell_pty_chunk';
    data: CellPtyChunkPayload;
}

export interface CellAstDiffPayload {
    session_id: string;
    iteration: number;
    path: string;
    search: string;
    replace: string;
}
export interface ServerCellAstDiffEvent {
    event_type: 'server_cell_ast_diff';
    data: CellAstDiffPayload;
}

export interface CellGovernorTickPayload {
    session_id: string;
    step: number;
    cost_usd: number;
    elapsed_s: number;
    axis?: string | null;
}
export interface ServerCellGovernorTickEvent {
    event_type: 'server_cell_governor_tick';
    data: CellGovernorTickPayload;
}

export interface CodeProposalPayload {
    filepath: string;
    proposed_content: string;
    base_document_version_id: string;
    agent_signature: string;
}
export interface ServerCodeProposalEvent {
    event_type: 'server_code_proposal';
    data: CodeProposalPayload;
}

export interface StatusPayload {
    agent_name: string;
    status_message: string;
    is_error: boolean;
}
export interface ServerStatusEvent {
    event_type: 'server_status';
    data: StatusPayload;
}

export interface StateCompactedPayload {
    session_id: string;
    compaction_message: string;
    turns_compressed: number;
}
/** Server event — note the `state_compacted` tag carries no `server_` prefix. */
export interface ServerStateCompactedEvent {
    event_type: 'state_compacted';
    data: StateCompactedPayload;
}

// ============================================================
// CLIENT → SERVER EVENTS (23)
// ============================================================

/** Ephemeral auth handshake — the one event with no `data` envelope. */
export interface AuthEvent {
    event_type: 'auth';
    token: string;
}

export interface FileUpdatePayload {
    filepath: string;
    content: string;
    document_version_id: string;
}
export interface ClientFileUpdateEvent {
    event_type: 'client_file_update';
    data: FileUpdatePayload;
}

export interface PlannerModeTogglePayload {
    active: boolean;
}
export interface ClientPlannerModeToggleEvent {
    event_type: 'client_planner_mode_toggle';
    data: PlannerModeTogglePayload;
}

export interface HITLResponsePayload {
    approval_id: string;
    approved: boolean;
    comment?: string | null;
    modified_content?: string | null;
}
export interface ClientHITLResponseEvent {
    event_type: 'client_hitl_response';
    data: HITLResponsePayload;
}

export interface RegisterSessionPayload {
    session_id: string;
}
export interface ClientRegisterSessionEvent {
    event_type: 'client_register_session';
    data: RegisterSessionPayload;
}

export interface ConcurrencyConflictPayload {
    filepath: string;
    expected_version: number;
    actual_version: number;
}
export interface ClientConcurrencyConflictEvent {
    event_type: 'client_concurrency_conflict';
    data: ConcurrencyConflictPayload;
}

export interface WorkspaceInitPayload {
    workspace_root: string;
    project_id: string;
    workspace_pid?: number | null;
}
export interface ClientWorkspaceInitEvent {
    event_type: 'client_workspace_init';
    data: WorkspaceInitPayload;
}

export interface FileDeletePayload {
    filepath: string;
    project_id: string;
}
export interface ClientFileDeleteEvent {
    event_type: 'client_file_delete';
    data: FileDeletePayload;
}

export interface IdeTelemetryPayload {
    action: 'file_saved' | 'file_created' | 'file_renamed';
    filepath: string;
    /** Set only on file_renamed. */
    old_path?: string | null;
    document_version_id: string;
}
export interface ClientIdeTelemetryEvent {
    event_type: 'client_ide_telemetry';
    data: IdeTelemetryPayload;
}

export interface DreamingRunPayload {
    /** Theme to prioritize; null/absent = Auto (whole workspace). */
    focus_area?: string | null;
}
export interface ClientDreamingRunEvent {
    event_type: 'client_dreaming_run';
    data: DreamingRunPayload;
}

export interface PatchAppliedPayload {
    patch_id: string;
    ok: boolean;
    applied_files: string[];
    stale_files: string[];
    error?: string | null;
}
export interface ClientPatchAppliedEvent {
    event_type: 'client_patch_applied';
    data: PatchAppliedPayload;
}

export interface MasterTogglePayload {
    enabled: boolean;
}
export interface ClientMasterToggleEvent {
    event_type: 'client_master_toggle';
    data: MasterTogglePayload;
}

export interface ProfileChangePayload {
    profile: 'Medium' | 'Big' | 'Cloud' | 'Hybrid';
}
export interface ClientProfileChangeEvent {
    event_type: 'client_profile_change';
    data: ProfileChangePayload;
}

export interface AnalystQueryPayload {
    text: string;
    session_id?: string | null;
    context_paths: string[];
    cursor?: number | null;
    model_tier?: string | null;
}
export interface ClientAnalystQueryEvent {
    event_type: 'client_analyst_query';
    data: AnalystQueryPayload;
}

export interface ClientClearConversationEvent {
    event_type: 'client_clear_conversation';
    data: Record<string, unknown>;
}

export interface RestoreHistoryPayload {
    messages: ChatTurn[];
}
export interface ClientRestoreHistoryEvent {
    event_type: 'client_restore_history';
    data: RestoreHistoryPayload;
}

export interface ClientInlineEditRequestPayload {
    edit_id: string;
    session_id: string;
    file_path: string;
    range_start: number;
    range_end: number;
    prompt: string;
    base_hash: string;
    selected_text: string;
    language_id?: string | null;
}
export interface ClientInlineEditRequestEvent {
    event_type: 'client_inline_edit_request';
    data: ClientInlineEditRequestPayload;
}

export interface ClientInlineEditCancelPayload {
    edit_id: string;
    session_id: string;
}
export interface ClientInlineEditCancelEvent {
    event_type: 'client_inline_edit_cancel';
    data: ClientInlineEditCancelPayload;
}

export interface ClientAbortMeshPayload {
    session_id: string;
}
export interface ClientAbortMeshEvent {
    event_type: 'client_abort_mesh';
    data: ClientAbortMeshPayload;
}

export interface ClientPtyWritePayload {
    session_id: string;
    data: string;
}
export interface ClientPtyWriteEvent {
    event_type: 'client_pty_write';
    data: ClientPtyWritePayload;
}

export interface ClientRetryToolPayload {
    session_id: string;
    tool_call_id: string;
}
export interface ClientRetryToolEvent {
    event_type: 'client_retry_tool';
    data: ClientRetryToolPayload;
}

export interface ClientInvokeTrackedBashPayload {
    session_id: string;
    command: string;
    timeout_sec: number;
    working_dir?: string | null;
}
export interface ClientInvokeTrackedBashEvent {
    event_type: 'client_invoke_tracked_bash';
    data: ClientInvokeTrackedBashPayload;
}

export interface ClientBranchFromCheckpointPayload {
    parent_session_id: string;
    from_checkpoint_id: string;
}
export interface ClientBranchFromCheckpointEvent {
    event_type: 'client_branch_from_checkpoint';
    data: ClientBranchFromCheckpointPayload;
}

// ============================================================
// DISCRIMINATED UNIONS
// ============================================================

/** Every server→client event (35). Narrow on `event_type`. */
export type ServerWSMessage =
    | ServerTokenChunkEvent
    | ServerThinkingChunkEvent
    | ServerTelemetryEvent
    | ServerGraphMutationEvent
    | ServerHITLApprovalRequestEvent
    | ServerModelWarmupEvent
    | ServerOomEngagedEvent
    | ServerIndexingProgressEvent
    | ServerIndexingErrorEvent
    | ServerByomConfigAppliedEvent
    | ServerVfsPatchApprovedEvent
    | ServerApplyWorkspaceEditEvent
    | ServerNattMessageEvent
    | ServerNattTokenChunkEvent
    | ServerNattStreamEndEvent
    | ServerPipelineStepEvent
    | ServerPlanDocumentEvent
    | ServerStreamEndEvent
    | ServerInlineEditStartEvent
    | ServerInlineEditDeltaEvent
    | ServerInlineEditEndEvent
    | ServerAbortAckEvent
    | ServerHitlAckEvent
    | ServerToolStartEvent
    | ServerToolStreamChunkEvent
    | ServerToolResultEvent
    | ServerToolDepGraphEvent
    | ServerSessionBranchedEvent
    | ServerCellToolStartEvent
    | ServerCellPtyChunkEvent
    | ServerCellAstDiffEvent
    | ServerCellGovernorTickEvent
    | ServerCodeProposalEvent
    | ServerStatusEvent
    | ServerStateCompactedEvent
    | ServerDevcontainerProvisionRequestEvent
    | ServerDevcontainerExecRequestEvent;

/** Every client→server event (23). */
export type ClientWSMessage =
    | AuthEvent
    | ClientFileUpdateEvent
    | ClientPlannerModeToggleEvent
    | ClientHITLResponseEvent
    | ClientRegisterSessionEvent
    | ClientConcurrencyConflictEvent
    | ClientWorkspaceInitEvent
    | ClientFileDeleteEvent
    | ClientIdeTelemetryEvent
    | ClientDreamingRunEvent
    | ClientPatchAppliedEvent
    | ClientMasterToggleEvent
    | ClientProfileChangeEvent
    | ClientAnalystQueryEvent
    | ClientClearConversationEvent
    | ClientRestoreHistoryEvent
    | ClientInlineEditRequestEvent
    | ClientInlineEditCancelEvent
    | ClientAbortMeshEvent
    | ClientPtyWriteEvent
    | ClientRetryToolEvent
    | ClientInvokeTrackedBashEvent
    | ClientBranchFromCheckpointEvent
    | ClientDevcontainerProvisionStatusEvent
    | ClientDevcontainerExecStreamEvent
    | ClientDevcontainerExecExitEvent;

/** The full bidirectional wire union. */
export type WSMessage = ServerWSMessage | ClientWSMessage;

/** Discriminant value of any server→client event. */
export type ServerEventType = ServerWSMessage['event_type'];

// The authoritative set of server→client discriminants — the single place the
// `state_compacted` exception is enumerated alongside the `server_*` tags. Kept
// adjacent to `ServerWSMessage` so the two are reviewed together.
const SERVER_EVENT_TYPES: ReadonlySet<string> = new Set<ServerEventType>([
    'server_token_chunk',
    'server_thinking_chunk',
    'server_telemetry',
    'server_graph_mutation',
    'server_hitl_approval_request',
    'server_model_warmup',
    'server_oom_engaged',
    'server_indexing_progress',
    'server_indexing_error',
    'server_byom_config_applied',
    'server_vfs_patch_approved',
    'server_apply_workspace_edit',
    'server_natt_message',
    'server_natt_token',
    'server_natt_stream_end',
    'server_pipeline_step',
    'server_plan_document',
    'server_stream_end',
    'server_inline_edit_start',
    'server_inline_edit_delta',
    'server_inline_edit_end',
    'server_abort_ack',
    'server_hitl_ack',
    'server_tool_start',
    'server_tool_stream_chunk',
    'server_tool_result',
    'server_tool_dep_graph',
    'server_session_branched',
    'server_cell_tool_start',
    'server_cell_pty_chunk',
    'server_cell_ast_diff',
    'server_cell_governor_tick',
    'server_code_proposal',
    'server_status',
    'state_compacted',
    'server_devcontainer_provision_request',
    'server_devcontainer_exec_request',
]);

/** Runtime narrowing guard: true when `m` is a server→client wire event. */
export function isServerEvent(m: unknown): m is ServerWSMessage {
    if (typeof m !== 'object' || m === null) { return false; }
    const tag = (m as { event_type?: unknown }).event_type;
    return typeof tag === 'string' && SERVER_EVENT_TYPES.has(tag);
}
