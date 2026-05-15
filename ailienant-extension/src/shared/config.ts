export type IntelligenceProfile = "Medium" | "Big" | "Cloud" | "Hybrid";

export const PROFILE_LABELS: Record<IntelligenceProfile, string> = {
    Medium: "Medium (local)",
    Big:    "Big (local heavy)",
    Cloud:  "Cloud",
    Hybrid: "Hybrid (auto)",
};

export const DEFAULT_PROFILE: IntelligenceProfile = "Hybrid";

// Discriminated union of every message the webview can post.
// Mirrors the cases in src/providers/chat_sidebar.ts onDidReceiveMessage.
export type WebviewToHostMessage =
    | { type: "SUBMIT_TASK";        value: string }
    | { type: "ABORT_TASK" }
    | { type: "togglePlannerMode";  value: boolean }
    | { type: "master_toggle";      value: boolean }
    | { type: "profile_change";     value: IntelligenceProfile };

export const WORKSPACE_STATE_KEYS = {
    masterEnabled: "ailienant.masterEnabled",
    profile:       "ailienant.intelligenceProfile",
} as const;
