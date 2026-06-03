export type IntelligenceProfile = "Medium" | "Big" | "Cloud" | "Hybrid";

export const PROFILE_LABELS: Record<IntelligenceProfile, string> = {
    Medium: "Medium (local)",
    Big:    "Big (local heavy)",
    Cloud:  "Cloud",
    Hybrid: "Hybrid (auto)",
};

export const DEFAULT_PROFILE: IntelligenceProfile = "Hybrid";

// --- Phase 7 types ---

export type ReasoningPreset = "surgeon" | "architect" | "explorer";

export type InferenceTier = "LOCAL_ONLY" | "HYBRID" | "SOLO_CLOUD";

export type DreamingProfile = "Medium" | "Big" | "Cloud" | "Hybrid";

export type AgentRole =
    | "core_dev"
    | "architect_refactor"
    | "devops_infra"
    | "secops"
    | "qa_tester"
    | "doc_manager"
    | "vcs_manager"
    | "data_ml_engineer"
    | "orchestrator";

export type WsConnectionStatus = "connected" | "reconnecting" | "disconnected";

export type BudgetLimitMode = 'weekly' | 'monthly' | 'none';

// 7.9.A.7 — Models menu. 'manual' pins one model (no routing); 'auto' uses tiered orchestration.
export type OrchestrationMode = 'manual' | 'auto';

export type OccStatus = "clear" | "soft_conflict" | "hard_conflict";

export interface TelemetryFrame {
    session_id: string;
    routing_decision: string;
    css_total: number;
    task_complexity_index: number;
    is_red_alert: boolean;
}

export interface TokenSnapshot {
    local_tokens: number;
    cloud_tokens: number;
    savings_pct: number;
    total_cost_usd: number;
    // Live context-window occupancy, merged in by the host from the per-thread
    // context route. Optional: a snapshot frame that predates the fetch (or a
    // cold thread) simply omits them and the meter hides itself.
    context_window?: number;
    context_used_tokens?: number;
}

// Phase 7.11.4 — @mention dropdown autocomplete result row.
export interface MentionItem {
    kind: 'file' | 'folder' | 'terminal';
    path: string;
}

// Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chip shape kept in `Message.toolCalls`.
// The frontend builds it up incrementally from the three `server_tool_*`
// events; the optional `dep_graph` arrives via `server_tool_dep_graph`.
export interface ToolCallShape {
    tool_call_id: string;
    tool_name: string;
    args: Record<string, unknown>;
    status: 'pending' | 'success' | 'error';
    output_lines: string[];           // appended in arrival order
    exit_code?: number;
    duration_ms?: number;
    side_effect_free?: boolean;
    dep_graph?: {
        nodes: { id: string; label: string }[];
        edges: { from: string; to: string }[];
    };
}

// A finalized plan, delivered as one structured `server_plan_document` message.
// Mirrors the backend PlanDocumentPayload — `summary` is the one-line chat pointer
// that travels WITH the structure so the bubble and the rich Plan panel render on
// a single state transition (no two-message ordering race).
export interface PlanWBSStep {
    step_number: number;
    target_role: string;
    action: string;
    target_file: string;
    description: string;
    status: string;
}
export interface PlanDocumentShape {
    summary: string;
    outcome: string;
    scope: string[];
    constraints: string[];
    decisions: string[];
    tasks: PlanWBSStep[];
    checks: string[];
    ubiquitous_language: Record<string, string>;
}

// One file's worth of inline diff, surfaced by the host once an approved edit is
// applied (both sides arrive EOL-normalized host-side). Attached to the assistant
// turn that explained the edit and rendered as a split diff.
export interface DiffBlockShape {
    patch_id: string;
    file_path: string;
    old_content: string;
    new_content: string;
    status: 'edit' | 'create';
}

// Discriminated union of every message the webview can post.
// Mirrors the cases in src/providers/chat_sidebar.ts onDidReceiveMessage.
export type WebviewToHostMessage =
    | { type: "SUBMIT_TASK";        value: string; preset?: ReasoningPreset; tier?: InferenceTier; planner_mode_active?: boolean }
    | { type: "ABORT_TASK" }
    | { type: "ABORT_MESH" }  // Phase 7.11.3 — Abort Controller Mesh (backend Task.cancel)
    | { type: "dreaming_toggle";    value: boolean; profile: DreamingProfile }
    | { type: "FORCE_AGENT";        role: AgentRole }
    | { type: "FILE_BLOCKED_ACK" }
    | { type: "SET_BUDGET_LIMIT"; mode: BudgetLimitMode; weeklyUsd: number; monthlyUsd: number }
    // Phase 7.11.4 — @mention autocomplete: query the host-side workspace trie.
    | { type: "WORKSPACE_PATHS_QUERY"; prefix: string }
    // Phase 7.11.4 — @terminal stub: open the existing ContextOverlay terminal tab.
    | { type: "OPEN_CONTEXT_TERMINAL" }
    // Phase 7.11.6 — Rich Tool Chips: Retry button on a chip.
    | { type: "RETRY_TOOL";          tool_call_id: string }
    // Phase 7.11.6 — Dev smoke command (palette `/dev/run-bash <cmd>`).
    | { type: "INVOKE_TRACKED_BASH"; command: string }
    // Phase 7.11.6 — Palette → host: prompt for the bash command via
    // VS Code's native showInputBox, then dispatch INVOKE_TRACKED_BASH.
    | { type: "PROMPT_FOR_BASH" }
    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: fork a session from a
    // specific historical checkpoint. The host relays this verbatim onto the
    // WS as `client_branch_from_checkpoint`; the backend mints the new
    // session_id and broadcasts `server_session_branched`.
    | { type: "BRANCH_FROM_CHECKPOINT"; session_id: string; checkpoint_id: string; message_index?: number }
    // Phase 7.11.8 — Palette → host: fetch the checkpoint chain for the
    // active session (REST GET) and open the CheckpointPicker overlay.
    | { type: "LIST_CHECKPOINTS"; session_id: string }
    // Rich Plan panel: a file-link click asks the host to open the file in the
    // editor. The path is workspace-relative and host-validated before opening.
    | { type: "OPEN_FILE"; path: string };

export const WORKSPACE_STATE_KEYS = {
    masterEnabled:   "ailienant.masterEnabled",
    profile:         "ailienant.intelligenceProfile",
    dreamingEnabled: "ailienant.dreamingEnabled",
    dreamingProfile: "ailienant.dreamingProfile",
    reasoningPreset:   "ailienant.reasoningPreset",
    inferenceTier:     "ailienant.inferenceTier",
    budgetLimitMode:   "ailienant.budgetLimitMode",
    budgetWeeklyUsd:   "ailienant.budgetWeeklyUsd",
    budgetMonthlyUsd:  "ailienant.budgetMonthlyUsd",
    activeModelId:     "ailienant.activeModelId",
    orchestrationMode: "ailienant.orchestrationMode",
} as const;
