// ============================================================
// AILIENANT — Cross-surface shared types
// Used by extension host, sidebar webview, workspace webview, dashboard SPA.
// ============================================================

export interface AilienantConfig {
    api_key?: string;
    engine_endpoint: string;
    agent_settings: {
        analyst_name: string;
    };
    tiers: {
        small: string;
        medium: string;
        big: string;
        cloud?: string;
    };
    finops?: {
        budget_usd?: number;
    };
}

export const DEFAULT_ANALYST_NAME = 'Natt';

export type ModelTier = 'small' | 'medium' | 'big' | 'cloud';

export type ExecutionMode = 'automatic' | 'ask_before_edits' | 'plan_mode';

export const EXECUTION_MODE_LABELS: Record<ExecutionMode, string> = {
    automatic:        'Auto',
    ask_before_edits: 'Ask',
    plan_mode:        'Plan',
};

export const EXECUTION_MODE_DESCRIPTIONS: Record<ExecutionMode, string> = {
    automatic:        'Just run — minimal interruptions',
    ask_before_edits: 'Confirm every file mutation',
    plan_mode:        'Analyze only — no execution',
};

export type IndexingState =
    | { state: 'idle' }
    | { state: 'indexing'; pct: number; files_indexed?: number; total_files?: number }
    | { state: 'ready'; node_count: number }
    | { state: 'error'; reason: string };

export interface Session {
    id: string;
    title: string;
    created_at: string;
    last_modified: string;
    message_count: number;
    model_tier: ModelTier;
    thread_id?: string;
    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: when this session was minted
    // by a branch op, these point to the parent thread and the source
    // checkpoint inside it (UUID4 hex strings). Used by the sidebar to render
    // a "↪ Branch of <parent>" subtitle and by future lineage walks.
    parent_thread_id?: string;
    parent_checkpoint_id?: string;
}

// ── Sidebar ↔ Extension IPC ────────────────────────────────
export type SidebarToExtMessage =
    | { type: 'NEW_SESSION' }
    | { type: 'OPEN_SESSION'; session_id: string }
    | { type: 'DELETE_SESSION'; session_id: string }
    | { type: 'RENAME_SESSION'; session_id: string; title: string };

export type ExtToSidebarMessage =
    | { type: 'SESSIONS_UPDATED'; sessions: Session[] }
    | { type: 'CONFIG_UPDATED'; config: AilienantConfig | null };

// ── Workspace ↔ Extension IPC ──────────────────────────────
export type WorkspaceToExtMessage =
    | { type: 'TASK_SUBMIT'; prompt: string; session_id: string }
    | { type: 'HITL_RESPONSE'; approval_id: string; approved: boolean; comment?: string; modified_content?: string }
    | { type: 'PRESET_CHANGE'; preset: 'surgeon' | 'architect' | 'explorer' }
    | { type: 'TIER_CHANGE'; tier: 'LOCAL_ONLY' | 'HYBRID' | 'SOLO_CLOUD' }
    | { type: 'DREAMING_TOGGLE'; active: boolean; profile?: string }
    | { type: 'ATTACH_CONTEXT'; kind: 'file' | 'terminal' | 'directory'; payload: string };

export type ExtToWorkspaceMessage =
    | { type: 'LOAD_SESSION'; session_id: string }
    | { type: 'CONFIG_UPDATED'; config: AilienantConfig | null }
    | { type: 'WS_EVENT'; event: unknown };

// ── Phase 7.9.A.7 — Command-menu config types ──────────────
export type OutputStyle = 'default' | 'concise' | 'explanatory' | 'code_only';
export type PermissionMode = 'default' | 'plan' | 'auto';
export type HookEvent = 'pre_patch' | 'post_patch';

export interface SystemSettings {
    analyst_name: string;
    output_style: OutputStyle;
    permission_mode: PermissionMode;
}

export interface Hook {
    id: string;
    event: HookEvent;
    command: string;
    enabled: boolean;
}

export interface McpServer {
    id: string;
    name: string;
    transport: string;
    uri: string;
    enabled: boolean;
}

export interface McpTestResult {
    reachable: boolean;
    tool_count: number;
    error?: string;
}

export interface SkillTemplate {
    id: string;
    name: string;
    body: string;
}

export interface AgentRoleInfo {
    role: string;
    base_prompt: string;
    override: string | null;
    editable: boolean;
}

// ── Natt pane state ───────────────────────────────────────
export type NattFocus = 'idle' | 'conversation' | 'hitl';

export interface NattState {
    open: boolean;
    focus: NattFocus;
    hitl_request?: {
        approval_id: string;
        action_proposed: string;
        risk_metrics: Array<{ label: string; level: 'low' | 'medium' | 'high' }>;
        proposed_content?: string;
    };
}
