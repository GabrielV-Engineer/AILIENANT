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
 *   - `inputDraft`            — in-progress prompt text
 *   - `paletteOpen`/`contextOpen` — PromptBar overlay state
 *   - `nattOpen`              — analyst pane open/closed
 *   - `coreMenuOpen`          — header core-status popover
 *   - `mode`/`preset`/`tier`  — ModeMenu local toggles (NOT host-persisted)
 *   - `lastScrollY`           — chat scroll position
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

export interface WorkspaceState {
    inputDraft: string;
    paletteOpen: boolean;
    contextOpen: boolean;
    nattOpen: boolean;
    coreMenuOpen: boolean;
    mode: ExecutionMode;
    preset: ReasoningPreset;
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

    // Setters (Zustand pattern — flat actions colocated with state).
    setInputDraft: (v: string) => void;
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
}

export const useWorkspaceStore = createPersistedStore<WorkspaceState>(
    (set) => ({
        inputDraft: '',
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

        setInputDraft:   (v) => set({ inputDraft: v }),
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
    }),
    {
        key: 'workspace.v1',
        version: 1,
        // Defensive whitelist — never persist anything not listed (plan W4).
        pick: (s) => ({
            inputDraft: s.inputDraft,
            paletteOpen: s.paletteOpen,
            contextOpen: s.contextOpen,
            nattOpen: s.nattOpen,
            coreMenuOpen: s.coreMenuOpen,
            mode: s.mode,
            preset: s.preset,
            tier: s.tier,
            lastScrollY: s.lastScrollY,
            nativeThinking: s.nativeThinking,
        }),
    },
);
