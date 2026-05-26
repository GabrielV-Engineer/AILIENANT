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

// Discriminated union of every message the webview can post.
// Mirrors the cases in src/providers/chat_sidebar.ts onDidReceiveMessage.
export type WebviewToHostMessage =
    | { type: "SUBMIT_TASK";        value: string; preset?: ReasoningPreset; tier?: InferenceTier }
    | { type: "ABORT_TASK" }
    | { type: "ABORT_MESH" }  // Phase 7.11.3 — Abort Controller Mesh (backend Task.cancel)
    | { type: "togglePlannerMode";  value: boolean }
    | { type: "master_toggle";      value: boolean }
    | { type: "profile_change";     value: IntelligenceProfile }
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
    | { type: "PROMPT_FOR_BASH" };

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
