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

export interface Session {
    id: string;
    title: string;
    created_at: string;
    last_modified: string;
    message_count: number;
    model_tier: ModelTier;
    thread_id?: string;
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
