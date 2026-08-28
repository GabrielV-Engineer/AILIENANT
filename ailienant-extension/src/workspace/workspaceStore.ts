/**
 * Phase 7.11.2 (ADR-706 §4.5c) — Workspace WebView store.
 *
 * Source of truth for the *tab-switch-survivable* UI slice of `Workspace.tsx`
 * and `PromptBar.tsx`. Backed by `acquireVsCodeApi().setState/getState` via the
 * `createPersistedStore` middleware — survives backgrounding the panel but NOT
 * a full VS Code restart (durable, cross-restart state lives in the host's
 * `workspaceState` per 7.9.B.20 and friends, untouched).
 *
 * Persistable slice (whitelisted in `pick`):
 *   - `draftMessages`         — in-progress prompt text, keyed by sessionId (Phase
 *                               7.12.9 Fix 5: drafts survive session switches)
 *   - `paletteOpen`/`contextOpen` — PromptBar overlay state
 *   - `nattOpen`              — analyst pane open/closed
 *   - `coreMenuOpen`          — header core-status popover
 *   - `mode`/`preset`/`tier`  — ModeMenu local toggles (NOT host-persisted)
 *   - `lastScrollY`           — chat scroll position
 *   - `inflightTurn`          — Phase 7.12: in-flight streaming turn snapshot
 *                               (display-only thinking resilience across reloads)
 *
 * Excluded (host-fed, live, or transient — NOT persisted): the live chat-runtime
 * slice lives in the memory-only `useChatStore` (`chatStore.ts`), which the WS
 * dispatch controller mutates — wsStatus, occStatus, telemetry, snapshot, indexing,
 * lockedFiles, config, workspaceFolder, budget*, messages, nattMessages,
 * hitlPending, isStreaming, activeTaskId, attachedItems, nattAttachedItems, toasts,
 * tps. Local component state retains activeModelId, orchestrationMode, dreaming*.
 */
import type { ExecutionMode } from '../shared/types';
import type { ReasoningPreset, InferenceTier, PlanWBSStep, TimelineEntry } from '../shared/config';
import type { AgentTodoItemPayload, CoderCompanionPayload } from '../api/contracts';
import { createPersistedStore } from '../shared/persistedStore';

/**
 * Shape-specific equality for the bounded (<=50 items, 3 short string fields)
 * agent_todos payload — deliberately not a generic deep-equal: the structure is
 * small and flat, so a purpose-built compare stays O(n) with no dependency and
 * no canonicalization step a hash-based approach would need.
 */
function _todosEqual(a: AgentTodoItemPayload[], b: AgentTodoItemPayload[]): boolean {
    if (a === b) { return true; }
    if (a.length !== b.length) { return false; }
    return a.every((item, i) =>
        item.content === b[i].content && item.status === b[i].status && item.active_form === b[i].active_form);
}

/**
 * Analyst answer-model tier, chosen in the Natt HUD. Maps to a model in the
 * active BYOM preset; trades speed vs quality and only affects generation
 * (retrieval/grounding is unchanged). Independent of the orchestration tier.
 */
export type AnalystTier = 'small' | 'medium' | 'big' | 'cloud';

/**
 * Snapshot of the active streaming assistant turn, kept so an in-flight turn
 * survives a panel teardown/reconnect (`retainContextWhenHidden:false`). A tab
 * switch destroys the webview before the debounced host-side
 * `PERSIST_TRANSCRIPT` can flush, and this `setState`-backed copy is the only
 * thing that survives that race — so whatever it omits is simply lost.
 *
 * Defined structurally (not imported from Workspace.tsx) to avoid a circular
 * type import; a `Message` is assignable to it.
 *
 * What it carries is decided by one rule: keep what nothing else can rebuild.
 * The reasoning text rides inside `timeline`'s own entries (the same stream the
 * message-scoped `thinking` field mirrors, which is why that field is absent
 * here rather than storing it twice), while the diff/cell/execution bodies are
 * stripped before the write — those other channels re-deliver. `parserState`
 * and `toolCalls` are omitted for the same reason: large, and reconstructible.
 * `thinkingStartedAt` is omitted because it cannot be honoured — it is a
 * `performance.now()` origin, and the teardown this snapshot exists for resets
 * that clock.
 *
 * The whole envelope is one `setState` slot shared with the user's draft, so
 * the spine is budget-trimmed on the way in (see `useSessionPersistence`).
 */
export interface InflightSnapshot {
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    thinkingTokens?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    steps?: string[];
    stepsDone?: boolean;
    checklist?: PlanWBSStep[];
    companions?: CoderCompanionPayload[];
    timeline?: TimelineEntry[];
}

/**
 * DEBT-124 — the fold boundary a `state_compacted` event establishes,
 * persisted so it survives a panel hide/reveal (the compaction MARKER rides a
 * transient `SystemMessage`, deliberately excluded from `PERSIST_TRANSCRIPT`,
 * while the underlying `ConversationMessage`s it folds away survive fine).
 * Anchored on `afterMessageId` — the stable id of the last message BEFORE the
 * marker — rather than an array index, since indices shift on rehydrate but a
 * message id does not. `markerId` lets a fresh compaction correctly supersede
 * a stale persisted one (the same "newer compaction re-folds" rule the live
 * marker path already follows in Workspace.tsx).
 */
export interface CompactionFold {
    markerId: string;
    afterMessageId: string | undefined;
    summaryText: string;
    turnsCompressed: number;
}

export interface WorkspaceState {
    /**
     * Phase 7.12.9 (Fix 5) — prompt drafts keyed by sessionId. Previously a single
     * global `inputDraft`, which leaked/wiped across session switches. Persisted so
     * a half-typed message survives a tab change and panel reload.
     */
    draftMessages: Record<string, string>;
    paletteOpen: boolean;
    contextOpen: boolean;
    nattOpen: boolean;
    coreMenuOpen: boolean;
    mode: ExecutionMode;
    preset: ReasoningPreset;
    /**
     * Routing tier. No longer user-selectable (reasoning presets drive routing),
     * but retained with its default so every `SUBMIT_TASK` payload keeps carrying
     * a tier and the backend contract is unchanged.
     */
    tier: InferenceTier;
    lastScrollY: number;
    /**
     * Phase 7.11.3 (ADR-706 §4.5b) — optimistic-feedback flag while a Stop is
     * in flight. Set true the instant the user clicks Stop; cleared by the
     * `server_stream_end` handler in Workspace.tsx. Transient — explicitly
     * EXCLUDED from `pick` so it never persists (a stale `true` after panel
     * reload would visually freeze the button).
     */
    isAborting: boolean;
    /**
     * Phase 9 (ADR-707) — Native Thinking master switch. ON by default to
     * prioritise high-reasoning capabilities natively; the user can opt out in
     * Command Palette → /models. Persisted (whitelisted in `pick`) so it
     * survives a panel reload, and injected into the SUBMIT_TASK payload so the
     * backend LLM gateway either appends `thinking:{type:"enabled",…}` (capable
     * models) or omits it for low-latency flat streaming.
     */
    nativeThinking: boolean;
    /**
     * Soft-permission switch: when ON, an incoming HITL approval whose risk
     * metrics are ALL low (or absent) is auto-approved without surfacing the
     * intervention card — a fast path for repetitive, low-risk edit flows. Any
     * medium/high-risk request still requires explicit authorization. OFF by
     * default (the safe posture); persisted so the choice survives reload.
     */
    autoAcceptLowRisk: boolean;
    /**
     * Analyst answer-model tier picked in the Natt HUD. Persisted so the choice
     * survives a reload, and sent on every NATT_MESSAGE. Defaults to 'medium'.
     * If the active preset lacks this tier the HUD resets it to a present one.
     */
    analystTier: AnalystTier;
    /**
     * Phase 7.12 — last snapshot of the in-flight streaming turn. Persisted so a
     * reconnect / tab re-reveal can rehydrate a partial Thought Box instead of
     * dropping it. Cleared (`null`) on `server_stream_end`. Display-only.
     */
    inflightTurn: InflightSnapshot | null;
    /**
     * DEBT-124 — the current fold boundary (see `CompactionFold`), if any.
     * Persisted like `inflightTurn` for the same reason: display-only durable
     * evidence that needs to survive a panel teardown/reveal even though the
     * live marker it mirrors is transient. `null` when no compaction has
     * fired this session.
     */
    compactionFold: CompactionFold | null;
    /**
     * Pending explicit skill chip per session — set when the user selects a skill
     * from the SkillsMenu; cleared on submit or manual removal. Transient:
     * excluded from `pick` (a stale chip after reload would silently inject a skill
     * the user forgot about).
     */
    activeSkills: Record<string, { id: string; name: string } | null>;
    /**
     * The agentic cell's live TODO list, keyed by session id. Transient — excluded
     * from `pick` (never persisted): a rehydrated list would describe work that
     * already finished by the time the panel reloads. Replace semantics mirror
     * the backend's `_merge_todos` reducer: an empty array is a real "clear the
     * panel" write, never "no opinion" (the anti-immortal-TODO invariant).
     */
    agentTodos: Record<string, AgentTodoItemPayload[]>;

    // Setters (Zustand pattern — flat actions colocated with state).
    setDraft: (sessionId: string, text: string) => void;
    /**
     * Bails on a deep-equal payload and keeps the SAME array reference in that
     * case, so Zustand's shallow-compare selector stops the re-render at the
     * store rather than the DOM — the client-side half of the emission-storm
     * guard (the server side already suppresses the redundant WS send).
     */
    setAgentTodos: (sessionId: string, todos: AgentTodoItemPayload[]) => void;
    setActiveSkill: (sessionId: string, v: { id: string; name: string } | null) => void;
    setPaletteOpen: (v: boolean) => void;
    setContextOpen: (v: boolean) => void;
    setNattOpen: (v: boolean) => void;
    setCoreMenuOpen: (v: boolean) => void;
    setMode: (v: ExecutionMode) => void;
    setPreset: (v: ReasoningPreset) => void;
    setTier: (v: InferenceTier) => void;
    setLastScrollY: (v: number) => void;
    setIsAborting: (v: boolean) => void;
    setNativeThinking: (v: boolean) => void;
    setAutoAcceptLowRisk: (v: boolean) => void;
    setAnalystTier: (v: AnalystTier) => void;
    setInflightTurn: (v: InflightSnapshot | null) => void;
    setCompactionFold: (v: CompactionFold | null) => void;
}

export const useWorkspaceStore = createPersistedStore<WorkspaceState>(
    (set) => ({
        draftMessages: {},
        paletteOpen: false,
        contextOpen: false,
        nattOpen: false,
        coreMenuOpen: false,
        mode: 'automatic',
        preset: 'architect',
        tier: 'HYBRID',
        lastScrollY: 0,
        isAborting: false,
        nativeThinking: true,
        autoAcceptLowRisk: false,
        analystTier: 'medium',
        inflightTurn: null,
        compactionFold: null,
        activeSkills: {},
        agentTodos: {},

        setDraft:        (sessionId, text) =>
            set((s) => ({ draftMessages: { ...s.draftMessages, [sessionId]: text } })),
        setActiveSkill:  (sessionId, v) =>
            set((s) => ({ activeSkills: { ...s.activeSkills, [sessionId]: v } })),
        setAgentTodos: (sessionId, todos) =>
            set((s) => {
                const prior = s.agentTodos[sessionId];
                if (prior && _todosEqual(prior, todos)) { return s; }
                return { agentTodos: { ...s.agentTodos, [sessionId]: todos } };
            }),
        setPaletteOpen:  (v) => set({ paletteOpen: v }),
        setContextOpen:  (v) => set({ contextOpen: v }),
        setNattOpen:     (v) => set({ nattOpen: v }),
        setCoreMenuOpen: (v) => set({ coreMenuOpen: v }),
        setMode:         (v) => set({ mode: v }),
        setPreset:       (v) => set({ preset: v }),
        setTier:         (v) => set({ tier: v }),
        setLastScrollY:  (v) => set({ lastScrollY: v }),
        setIsAborting:   (v) => set({ isAborting: v }),
        setNativeThinking: (v) => set({ nativeThinking: v }),
        setAutoAcceptLowRisk: (v) => set({ autoAcceptLowRisk: v }),
        setAnalystTier:  (v) => set({ analystTier: v }),
        setInflightTurn: (v) => set({ inflightTurn: v }),
        setCompactionFold: (v) => set({ compactionFold: v }),
    }),
    {
        key: 'workspace.v1',
        // Phase 7.12.9 (Fix 5) — bumped to v2: the persisted shape changed
        // (inputDraft:string → draftMessages:Record). A version mismatch safely
        // discards the old v1 payload on hydrate (persistedStore.ts:70).
        version: 2,
        // Defensive whitelist — never persist anything not listed (plan W4).
        pick: (s) => ({
            draftMessages: s.draftMessages,
            paletteOpen: s.paletteOpen,
            contextOpen: s.contextOpen,
            nattOpen: s.nattOpen,
            coreMenuOpen: s.coreMenuOpen,
            mode: s.mode,
            preset: s.preset,
            tier: s.tier,
            lastScrollY: s.lastScrollY,
            nativeThinking: s.nativeThinking,
            autoAcceptLowRisk: s.autoAcceptLowRisk,
            analystTier: s.analystTier,
            inflightTurn: s.inflightTurn,
            compactionFold: s.compactionFold,
        }),
    },
);
