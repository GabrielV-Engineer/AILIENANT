/**
 * Live chat-runtime store for the workspace panel — the source of truth the WS
 * dispatch controller mutates and the layout host subscribes to.
 *
 * MEMORY-ONLY by design. Unlike `useWorkspaceStore` (which persists its UI slice
 * to `vscode.setState`), this store is deliberately NOT persisted: it holds
 * host-fed, live, and transient state (`messages`, `telemetry`, streaming flags,
 * …) that changes on every token. Serializing the transcript to `vscode.setState`
 * on each token would blow the setState quota and duplicate the host's own
 * transcript persistence (PERSIST_TRANSCRIPT / REHYDRATE). Durable transcript
 * survival lives in the host's `workspaceState`, not here.
 *
 * Setters mirror React's `Dispatch<SetStateAction<T>>` signature so the dispatch
 * and callback call sites read identically to the prior `useState` setters
 * (`setMessages(prev => …)` still works).
 */
import { create } from 'zustand';
import type { Dispatch, SetStateAction } from 'react';
import type {
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot,
    BudgetLimitMode, PlanDocumentShape,
} from '../shared/config';
import type { AilienantConfig, IndexingState } from '../shared/types';
import type { HITLIntervention } from './components/HITLInterventionCard';
import type { CheckpointEntry } from './components/CheckpointPicker';
import type {
    Message, NattMessage, AttachedItem, ToastItem, ToastLevel, InitialState,
} from './types';

// Monotonic toast id + one-shot hydrate guard. Module scope is correct: the store
// is a singleton per webview JS context, and a panel reload spins a fresh context.
let _toastId = 0;
let _hydrated = false;

/** Resolve a `SetStateAction` against the previous value (updater or bare value). */
const apply = <T>(v: SetStateAction<T>, prev: T): T =>
    typeof v === 'function' ? (v as (p: T) => T)(prev) : v;

export interface ChatState {
    messages: Message[];
    isStreaming: boolean;
    wsStatus: WsConnectionStatus;
    nattMessages: NattMessage[];
    hitlPending: HITLIntervention | undefined;
    config: AilienantConfig | null;
    telemetry: TelemetryFrame | undefined;
    occStatus: OccStatus;
    lockedFiles: number;
    snapshot: TokenSnapshot | undefined;
    indexing: IndexingState;
    activeTaskId: string | undefined;
    checkpointPicker: CheckpointEntry[] | null;
    budgetLimitMode: BudgetLimitMode;
    budgetWeeklyUsd: number;
    budgetMonthlyUsd: number;
    workspaceFolder: string;
    attachedItems: AttachedItem[];
    nattAttachedItems: AttachedItem[];
    plan: PlanDocumentShape | null;
    toasts: ToastItem[];
    tps: number;

    setMessages: Dispatch<SetStateAction<Message[]>>;
    setIsStreaming: Dispatch<SetStateAction<boolean>>;
    setWsStatus: Dispatch<SetStateAction<WsConnectionStatus>>;
    setNattMessages: Dispatch<SetStateAction<NattMessage[]>>;
    setHitlPending: Dispatch<SetStateAction<HITLIntervention | undefined>>;
    setConfig: Dispatch<SetStateAction<AilienantConfig | null>>;
    setTelemetry: Dispatch<SetStateAction<TelemetryFrame | undefined>>;
    setOccStatus: Dispatch<SetStateAction<OccStatus>>;
    setLockedFiles: Dispatch<SetStateAction<number>>;
    setSnapshot: Dispatch<SetStateAction<TokenSnapshot | undefined>>;
    setIndexing: Dispatch<SetStateAction<IndexingState>>;
    setActiveTaskId: Dispatch<SetStateAction<string | undefined>>;
    setCheckpointPicker: Dispatch<SetStateAction<CheckpointEntry[] | null>>;
    setBudgetLimitMode: Dispatch<SetStateAction<BudgetLimitMode>>;
    setBudgetWeeklyUsd: Dispatch<SetStateAction<number>>;
    setBudgetMonthlyUsd: Dispatch<SetStateAction<number>>;
    setWorkspaceFolder: Dispatch<SetStateAction<string>>;
    setAttachedItems: Dispatch<SetStateAction<AttachedItem[]>>;
    setNattAttachedItems: Dispatch<SetStateAction<AttachedItem[]>>;
    setPlan: Dispatch<SetStateAction<PlanDocumentShape | null>>;
    setTps: Dispatch<SetStateAction<number>>;

    /** Transient notification — auto-dismisses after 6s; capped at 3 visible. */
    addToast: (level: ToastLevel, message: string) => void;
    /** One-shot seed from the host's creation-time snapshot (idempotent). */
    hydrate: (initial: InitialState) => void;
}

export const useChatStore = create<ChatState>((set) => ({
    messages: [],
    isStreaming: false,
    wsStatus: 'disconnected',
    nattMessages: [],
    hitlPending: undefined,
    config: null,
    telemetry: undefined,
    occStatus: 'clear',
    lockedFiles: 0,
    snapshot: undefined,
    indexing: { state: 'idle' },
    activeTaskId: undefined,
    checkpointPicker: null,
    budgetLimitMode: 'none',
    budgetWeeklyUsd: 0,
    budgetMonthlyUsd: 0,
    workspaceFolder: '',
    attachedItems: [],
    nattAttachedItems: [],
    plan: null,
    toasts: [],
    tps: 0,

    setMessages:          (v) => set((s) => ({ messages: apply(v, s.messages) })),
    setIsStreaming:       (v) => set((s) => ({ isStreaming: apply(v, s.isStreaming) })),
    setWsStatus:          (v) => set((s) => ({ wsStatus: apply(v, s.wsStatus) })),
    setNattMessages:      (v) => set((s) => ({ nattMessages: apply(v, s.nattMessages) })),
    setHitlPending:       (v) => set((s) => ({ hitlPending: apply(v, s.hitlPending) })),
    setConfig:            (v) => set((s) => ({ config: apply(v, s.config) })),
    setTelemetry:         (v) => set((s) => ({ telemetry: apply(v, s.telemetry) })),
    setOccStatus:         (v) => set((s) => ({ occStatus: apply(v, s.occStatus) })),
    setLockedFiles:       (v) => set((s) => ({ lockedFiles: apply(v, s.lockedFiles) })),
    setSnapshot:          (v) => set((s) => ({ snapshot: apply(v, s.snapshot) })),
    setIndexing:          (v) => set((s) => ({ indexing: apply(v, s.indexing) })),
    setActiveTaskId:      (v) => set((s) => ({ activeTaskId: apply(v, s.activeTaskId) })),
    setCheckpointPicker:  (v) => set((s) => ({ checkpointPicker: apply(v, s.checkpointPicker) })),
    setBudgetLimitMode:   (v) => set((s) => ({ budgetLimitMode: apply(v, s.budgetLimitMode) })),
    setBudgetWeeklyUsd:   (v) => set((s) => ({ budgetWeeklyUsd: apply(v, s.budgetWeeklyUsd) })),
    setBudgetMonthlyUsd:  (v) => set((s) => ({ budgetMonthlyUsd: apply(v, s.budgetMonthlyUsd) })),
    setWorkspaceFolder:   (v) => set((s) => ({ workspaceFolder: apply(v, s.workspaceFolder) })),
    setAttachedItems:     (v) => set((s) => ({ attachedItems: apply(v, s.attachedItems) })),
    setNattAttachedItems: (v) => set((s) => ({ nattAttachedItems: apply(v, s.nattAttachedItems) })),
    setPlan:              (v) => set((s) => ({ plan: apply(v, s.plan) })),
    setTps:               (v) => set((s) => ({ tps: apply(v, s.tps) })),

    addToast: (level, message) => {
        const id = ++_toastId;
        set((s) => ({ toasts: [...s.toasts.slice(-2), { id, level, message }] }));
        setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 6000);
    },

    hydrate: (initial) => {
        if (_hydrated) { return; }
        _hydrated = true;
        set({
            config: initial.config,
            budgetLimitMode: initial.budgetLimitMode,
            budgetWeeklyUsd: initial.budgetWeeklyUsd,
            budgetMonthlyUsd: initial.budgetMonthlyUsd,
            workspaceFolder: initial.workspaceFolder,
            messages: initial.initialMessages ?? [],
            nattMessages: initial.initialNattMessages ?? [],
        });
    },
}));
