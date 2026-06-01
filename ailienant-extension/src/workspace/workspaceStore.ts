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
 * Excluded (host-fed, live, or transient — leave as `useState`):
 *   - wsStatus, occStatus, telemetry, snapshot, indexing, lockedFiles, config,
 *     workspaceFolder, activeModelId, orchestrationMode, budget*, dreaming*,
 *     messages, nattMessages, hitlPending, isStreaming, activeTaskId,
 *     attachedItems, nattAttachedItems, toasts.
 */
import type { ExecutionMode } from '../shared/types';
import type { ReasoningPreset, InferenceTier } from '../shared/config';
import { createPersistedStore } from '../shared/persistedStore';

/**
 * Top-level interaction surface. Derived from the execution `mode` (the HUD is
 * the single source of truth): `plan_mode` yields `planner`, every other mode
 * yields `chat`. The `planner` surface swaps the standard composer for the
 * Socratic ideation form and tags each submit with `planner_mode_active`,
 * routing the turn into the backend `ideation_loop`. Not stored — recomputed
 * from `mode` on every render so the two can never drift.
 */
export type WorkspaceSurface = 'chat' | 'planner';

/**
 * Phase 7.12 — minimal snapshot of the active streaming assistant turn, kept so
 * an in-flight Native-Thinking trace (display-only, ADR-707) survives a panel
 * teardown/reconnect (retainContextWhenHidden:false). Defined structurally (not
 * imported from Workspace.tsx) to avoid a circular type import; a `Message` is
 * assignable to it. `parserState`/`toolCalls` are intentionally omitted to keep
 * the persisted blob small.
 */
export interface InflightSnapshot {
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    steps?: string[];
    stepsDone?: boolean;
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
     * Phase 7.12 — last snapshot of the in-flight streaming turn. Persisted so a
     * reconnect / tab re-reveal can rehydrate a partial Thought Box instead of
     * dropping it. Cleared (`null`) on `server_stream_end`. Display-only.
     */
    inflightTurn: InflightSnapshot | null;

    // Setters (Zustand pattern — flat actions colocated with state).
    setDraft: (sessionId: string, text: string) => void;
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
    setInflightTurn: (v: InflightSnapshot | null) => void;
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
        inflightTurn: null,

        setDraft:        (sessionId, text) =>
            set((s) => ({ draftMessages: { ...s.draftMessages, [sessionId]: text } })),
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
        setInflightTurn: (v) => set({ inflightTurn: v }),
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
            inflightTurn: s.inflightTurn,
        }),
    },
);
