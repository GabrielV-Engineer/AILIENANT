import { useState, useCallback, useEffect, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import * as Popover from '@radix-ui/react-popover';
import { vscode } from './vscode_bridge';
import { useWorkspaceStore } from './workspaceStore';
import {
    BudgetLimitMode, ReasoningPreset, DreamingProfile,
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot, OrchestrationMode,
    ToolCallShape, DiffBlockShape, PlanDocumentShape, MAX_IPC_CODE_CHARS,
    type ASTToken, type CellRunShape, type CellIterationShape, type PlanWBSStep,
} from '../shared/config';
import type { AilienantConfig, ExecutionMode, IndexingState } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { Icon } from '../shared/Icon';
import { Tooltip as AiTooltip } from '../shared/Tooltip';
import { WorkspaceHeader } from './components/WorkspaceHeader';
import { TelemetryHUD, useTpsCalculator } from './components/TelemetryHUD';
import { CSSAlertBanner } from './components/CSSAlertBanner';
import { PromptBar } from './components/PromptBar';
import { NattCanvas } from './components/NattCanvas';
import { MarkdownRenderer } from './components/MarkdownRenderer';
import { ThoughtBox } from './components/ThoughtBox';
import { accumulateThinking, newThinkingTurn, freezeThinkingOnText, bumpLiveTokens } from './utils/thinkingReducer';
import { mergeStreamEmits, type StreamLineEmit } from './utils/streamTokenBuffer';
import { ToolChip } from './components/ToolChip';
import { DiffBlock } from './components/DiffBlock';
import { MessageActions } from './components/MessageActions';
import { CheckpointPicker, type CheckpointEntry } from './components/CheckpointPicker';
import {
    INITIAL_STATE as MD_INITIAL_STATE,
    pushToken as mdPushToken,
    extractCodeBlocks,
    type ParserState as MdParserState,
} from './utils/StreamingMarkdownParser';
import { IndexingStatus } from './components/IndexingStatus';
import { PipelineProgress } from './components/PipelineProgress';
import { PlanAcceptancePanel } from './components/PlanAcceptancePanel';
import { ActionLog } from './components/ActionLog';
import { CellAuditWidget } from './components/CellAuditWidget';
import { ExecutionChecklist } from './components/ExecutionChecklist';
import { sanitizePtyChunk } from './utils/sanitizePty';
import { HITLInterventionCard, type HITLIntervention } from './components/HITLInterventionCard';
import { useHitlResponder } from './utils/useHitlResponder';
import { getPresetConfig } from './hooks/useReasoningPreset';
import { ErrorBoundary } from './components/ErrorBoundary';

/**
 * Inline fallback for a single message row that throws during render. A malformed
 * turn degrades to this quiet placeholder instead of crashing the whole transcript.
 */
function MessageRowFallback(): JSX.Element {
    return (
        <div className="ws-error-row" role="alert">
            ⚠ This message could not be displayed.
        </div>
    );
}

type ToastLevel = 'info' | 'warn' | 'error';
interface ToastItem { id: number; level: ToastLevel; message: string; }
let _toastId = 0;

/**
 * The literal phrase that flips the backend Socratic loop into synthesis.
 * `analyst._is_agreement` does a case-insensitive substring match against its
 * `_AGREEMENT_SIGNALS` frozenset; this phrase matches both "looks good" and
 * "proceed". It is the sole frontend↔backend coupling point for the Planner
 * handoff — keep it in sync with that set.
 */
const AGREEMENT_SIGNAL = 'Looks good, proceed.';

// Pre-first-submit fallback only — the live timeout arrives from the backend via
// STREAM_WATCHDOG_MS (governed per active model: longer for slow local engines).
// It is never a hardcoded product timeout.
const DEFAULT_STREAM_WATCHDOG_MS = 90_000;
// How often the watchdog checks for a stalled stream.
const STREAM_WATCHDOG_TICK_MS = 5_000;
// Hard bound on a single tool chip's retained output (OOM guard for a runaway tool).
const MAX_TOOL_OUTPUT_LINES = 500;
// Server events that count as live stream activity (reset the stall watchdog).
const STREAM_ACTIVITY_EVENTS = new Set<string>([
    'server_token_chunk', 'server_thinking_chunk', 'server_pipeline_step',
    'server_tool_start', 'server_tool_stream_chunk', 'server_tool_result',
    'server_natt_token',
    'server_cell_tool_start', 'server_cell_pty_chunk', 'server_cell_ast_diff',
    'server_cell_governor_tick', 'server_graph_mutation',
]);
// Hard cap on retained PTY lines per cell iteration. On overflow the buffer stops
// appending and writes a single truncation sentinel, so the virtualized list's base
// indices never shift under the user's scroll.
const MAX_CELL_PTY_LINES = 5000;
const CELL_PTY_TRUNCATED = '[… Output truncated due to length …]';

/**
 * Flip any still-`pending` tool chip on a NON-streaming turn to `error`. A chip
 * left pending at a teardown/stall would otherwise rehydrate spinning forever.
 */
function normalizeStuckChips<T extends { streaming?: boolean; toolCalls?: ToolCallShape[] }>(msgs: T[]): T[] {
    return msgs.map(m => {
        if (m.streaming || !m.toolCalls || m.toolCalls.length === 0) { return m; }
        const fixed = m.toolCalls.map(tc =>
            (tc.status === undefined || tc.status === 'pending')
                ? { ...tc, status: 'error' as const }
                : tc);
        return { ...m, toolCalls: fixed };
    });
}

// Phase 7.12 — client-minted stable turn id. `crypto.randomUUID` is available in
// the webview runtime; the fallback keeps types honest on exotic hosts.
function mkId(): string {
    try { return crypto.randomUUID(); }
    catch { return `m_${Date.now()}_${Math.random().toString(36).slice(2)}`; }
}

// Resolves the display label for a turn at the moment it is first minted.
// Frozen onto Message.authorLabel so the row component stays pure and a later
// settings change never retroactively relabels existing history.
function authorLabelFor(role: 'user' | 'assistant', agentName: string): string {
    return role === 'user' ? 'You' : agentName;
}

// On stream-end, ask the host to syntax-highlight this turn's fenced code blocks.
// The webview holds no grammar engine, so it ships each block (lang + code) to the
// host lexer and later paints the returned tokens. A pre-IPC circuit breaker skips
// oversized blocks (never serialized — protects the isolate boundary from an O(N)
// copy); blocks with no language hint can't be tokenized and are skipped too.
function requestCodeTokens(turnId: string, content: string): void {
    const blocks = extractCodeBlocks(content).filter(
        (b) => b.lang.length > 0 && b.code.length > 0 && b.code.length <= MAX_IPC_CODE_CHARS,
    );
    if (blocks.length === 0) { return; }
    vscode.postMessage({
        type: 'TOKENIZE_CODE',
        turn_id: turnId,
        request_id: mkId(),
        blocks: blocks.map((b) => ({ hash: b.hash, lang: b.lang, code: b.code })),
    });
}

/**
 * Phase 7.12 — id-keyed transcript merge for REHYDRATE_TRANSCRIPT. The host
 * transcript is the authoritative COMPLETED history; `local` may hold an
 * in-flight turn the host hasn't persisted yet. Merge by stable `id` — never a
 * length heuristic (fragile under mid-stream tab-switches → state tearing):
 *   • host order is preserved as the spine;
 *   • a still-`streaming` local copy wins for a matching id (live content is
 *     fresher than the debounced host snapshot);
 *   • local turns with an id absent from host are appended (brand-new in-flight).
 */
function mergeById<T extends { id?: string; streaming?: boolean }>(host: T[], local: T[]): T[] {
    const hostIds = new Set(host.map(m => m.id).filter(Boolean));
    const spine = host.map(m => {
        const liveLocal = m.id ? local.find(l => l.id === m.id && l.streaming) : undefined;
        return liveLocal ?? m;
    });
    const tail = local.filter(m => m.id && m.streaming && !hostIds.has(m.id));
    return [...spine, ...tail];
}

/** Rich conversation turn (user or assistant). All fields beyond the three required
 *  ones are optional — most are ephemeral display data excluded from PERSIST_TRANSCRIPT. */
export interface ConversationMessage {
    // Stable per-turn id (client-minted via crypto.randomUUID at creation). Keys the
    // REHYDRATE_TRANSCRIPT merge so a tab re-reveal never clobbers a live in-flight turn.
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    steps?: string[];      // pipeline node trace for this assistant turn
    stepsDone?: boolean;   // true after server_stream_end → ✓ + auto-collapse
    // Incremental markdown parser state — live only while streaming; cleared on
    // server_stream_end so the renderer's stable fast path takes over. Transient —
    // explicitly stripped before PERSIST_TRANSCRIPT.
    parserState?: MdParserState;
    // Host-tokenized syntax spans keyed by hashCodeBlock(lang, code). Ephemeral.
    codeTokens?: Record<string, ASTToken[][]>;
    // Per-line streaming AST overlay; superseded by codeTokens on stream-end. Ephemeral.
    streamingCodeTokens?: Record<number, ASTToken[][]>;
    // Tool-chip artifacts built incrementally from server_tool_* events.
    toolCalls?: ToolCallShape[];
    // Glass-box agentic-cell telemetry. Display-only; stripped before PERSIST_TRANSCRIPT.
    cellRun?: CellRunShape;
    // Progressive WBS checklist — durable audit evidence, carried through PERSIST_TRANSCRIPT.
    checklist?: PlanWBSStep[];
    // Inline diff blocks surfaced by the host after PatchActuator applies. Persisted.
    diffBlocks?: DiffBlockShape[];
    // L2-promoted checkpoint wrapping this completed assistant turn. Persisted.
    checkpoint_id?: string;
    // Flips the branch button's icon to ⏹ for user_abort emergency savepoints.
    is_abort_savepoint?: boolean;
    // Frozen at ingestion; the row component is pure and never reads reactive config.
    authorLabel?: string;
    // Native Thinking (ADR-707) — raw reasoning. Display-only; NEVER persisted.
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    // Ghost Telemetry (ADR-723) — answer tokens tallied client-side. Persisted.
    liveTokens?: number;
}

/** Transient system notification chip — rendered in-transcript but never persisted.
 *  `streaming` and `toolCalls` are typed `never` (structurally absent) so the union
 *  satisfies the `normalizeStuckChips` / `mergeById` generic constraints without
 *  making these fields accidentally accessible at their sites. */
export interface SystemMessage {
    id?: string;
    role: 'system';
    content: string;
    readonly streaming?: never;
    readonly toolCalls?: never;
}

/** Discriminated union over `role`. System chips are filtered from PERSIST_TRANSCRIPT. */
export type Message = ConversationMessage | SystemMessage;
export interface NattMessage {
    id?: string;   // Phase 7.12 — see Message.id (REHYDRATE_TRANSCRIPT merge key).
    role: 'natt' | 'user';
    content: string;
    streaming?: boolean;
    // Same parser state, applied to analyst-canvas turns.
    parserState?: MdParserState;
}

/**
 * Phase 7.11.6 — Update the tool-chip artifact for `tool_call_id` on the LAST
 * assistant message (creating a placeholder turn if none exists yet). The
 * updater receives the prior chip (or `undefined` if this is the chip's first
 * event) and returns the next chip shape. Pure on the previous `messages`
 * array — no mutations.
 */
function attachOrUpdateToolCall(
    prev: Message[],
    toolCallId: string,
    update: (prior: ToolCallShape | undefined) => ToolCallShape,
    agentName: string,
): Message[] {
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    // Always attach to the last assistant turn — even if its streaming flag
    // has been cleared. The /dev/run-bash smoke command also lands here on a
    // fresh placeholder when no assistant turn is active yet.
    if (last?.role === 'assistant') {
        const calls = last.toolCalls ?? [];
        const idx = calls.findIndex(c => c.tool_call_id === toolCallId);
        const nextCall = update(idx >= 0 ? calls[idx] : undefined);
        const nextCalls = idx >= 0
            ? [...calls.slice(0, idx), nextCall, ...calls.slice(idx + 1)]
            : [...calls, nextCall];
        return [...prev.slice(0, lastIdx), { ...last, toolCalls: nextCalls }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        toolCalls: [update(undefined)],
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Update the cell-audit iteration for `iteration` on the LAST assistant message
 * (creating a placeholder turn if none exists yet). The updater receives the prior
 * iteration record (or a fresh empty one) and returns the next shape. Pure on the
 * previous `messages` array — no mutations.
 */
function attachOrUpdateCellRun(
    prev: Message[],
    iteration: number,
    update: (prior: CellIterationShape) => CellIterationShape,
    agentName: string,
): Message[] {
    const empty = (): CellIterationShape => ({ iteration, tools: [], pty: [], diffs: [] });
    const applyTo = (run: CellRunShape | undefined): CellRunShape => {
        const iters = run?.iterations ?? [];
        const idx = iters.findIndex((it) => it.iteration === iteration);
        const nextIter = update(idx >= 0 ? iters[idx] : empty());
        const nextIters = idx >= 0
            ? [...iters.slice(0, idx), nextIter, ...iters.slice(idx + 1)]
            : [...iters, nextIter].sort((a, b) => a.iteration - b.iteration);
        return { iterations: nextIters };
    };
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, cellRun: applyTo(last.cellRun) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        cellRun: applyTo(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Append sanitized PTY lines to an iteration's buffer with a stop-at-cap policy.
 * Once the cap is hit the buffer is frozen (apart from a one-time truncation
 * sentinel) so the virtualized list's indices never shift mid-scroll.
 */
function appendPtyLines(it: CellIterationShape, lines: string[]): CellIterationShape {
    if (it._truncated) { return it; }
    if (it.pty.length + lines.length <= MAX_CELL_PTY_LINES) {
        return { ...it, pty: [...it.pty, ...lines] };
    }
    const room = Math.max(0, MAX_CELL_PTY_LINES - it.pty.length);
    return { ...it, pty: [...it.pty, ...lines.slice(0, room), CELL_PTY_TRUNCATED], _truncated: true };
}

/**
 * Update the execution checklist on the LAST assistant message (creating a
 * placeholder turn if none exists). The updater receives the prior checklist (or
 * undefined) and returns the next one. Pure on the previous array — no mutations.
 */
function attachOrUpdateChecklist(
    prev: Message[],
    update: (prior: PlanWBSStep[] | undefined) => PlanWBSStep[],
    agentName: string,
): Message[] {
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, checklist: update(last.checklist) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        checklist: update(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}
interface AttachedItem { id: string; path: string; kind: 'file' | 'directory'; }

interface InitialState {
    sessionId:        string;
    sessionTitle:     string;
    config:           AilienantConfig | null;
    logoUri:          string;
    budgetLimitMode:  BudgetLimitMode;
    budgetWeeklyUsd:  number;
    budgetMonthlyUsd: number;
    activeModelId:    string;
    orchestrationMode: OrchestrationMode;
    workspaceFolder:  string;
    initialMessages?:     Message[];      // Phase 7.9.B.20 — restored chat transcript
    initialNattMessages?: NattMessage[];  // Phase 7.9.B.20 — restored analyst transcript
}

export function Workspace({ initial }: { initial: InitialState }): JSX.Element {
    const [config, setConfig] = useState<AilienantConfig | null>(initial.config);
    const nattName = config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;

    // Mode / preset (live inside ModeMenu) — Phase 7.11.2: persisted
    // panel-lifetime via workspaceStore (rehydrates on tab-switch).
    const mode    = useWorkspaceStore((s) => s.mode);
    const setMode = useWorkspaceStore((s) => s.setMode);
    const preset    = useWorkspaceStore((s) => s.preset);
    const setPreset = useWorkspaceStore((s) => s.setPreset);
    // Routing tier is no longer user-selectable (presets drive routing); the
    // value persists with its default and still rides every SUBMIT_TASK payload.
    const tier    = useWorkspaceStore((s) => s.tier);

    // Dreaming
    const [dreamingActive, setDreamingActive] = useState(false);
    const [dreamingProfile, setDreamingProfile] = useState<DreamingProfile>('Hybrid');

    // Models menu preferences (persisted via SET_MODEL_PREFERENCE)
    const [activeModelId, setActiveModelId] = useState<string>(initial.activeModelId ?? '');
    const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(initial.orchestrationMode ?? 'auto');

    // Budget — global persistent setting, not per-session
    const [budgetLimitMode,  setBudgetLimitMode]  = useState<BudgetLimitMode>(initial.budgetLimitMode);

    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: transient overlay state.
    // When non-null, renders the CheckpointPicker on top of the chat. Reset
    // to null on Esc / pick / SESSION_BRANCHED. Never persisted.
    const [checkpointPicker, setCheckpointPicker] = useState<CheckpointEntry[] | null>(null);
    const [budgetWeeklyUsd,  setBudgetWeeklyUsd]  = useState<number>(initial.budgetWeeklyUsd);
    const [budgetMonthlyUsd, setBudgetMonthlyUsd] = useState<number>(initial.budgetMonthlyUsd);
    const budgetUsd = budgetLimitMode === 'weekly'  ? budgetWeeklyUsd
                    : budgetLimitMode === 'monthly' ? budgetMonthlyUsd : 0;

    // Chat — Phase 7.9.B.20: restored from the persisted per-session transcript.
    const [messages, setMessages] = useState<Message[]>(initial.initialMessages ?? []);
    // Latest committed turns, mirrored for the WS handler (which closes over state
    // once). Synced in the render body so it is always current when an event fires.
    const messagesRef = useRef(messages);
    messagesRef.current = messages;
    const [isStreaming, setIsStreaming] = useState(false);

    // Phase 7.17.1 — streaming-AST hydration buffer. Per-line STREAM_CODE_TOKENS
    // pushes are coalesced into a single setMessages per animation frame, so the
    // progressive highlighting doesn't fire one full-transcript reconciliation per
    // code line. The buffer is stamped with its target turn id; a frame that lands
    // after a turn boundary is dropped rather than merged into the wrong turn.
    const streamTokenBufferRef = useRef<{ turnId: string; emits: StreamLineEmit[] } | null>(null);
    const streamTokenRafRef = useRef<number | null>(null);
    const flushStreamTokens = useCallback(() => {
        streamTokenRafRef.current = null;
        const buffered = streamTokenBufferRef.current;
        streamTokenBufferRef.current = null;
        if (!buffered || buffered.emits.length === 0) { return; }
        setMessages(prev => {
            const last = prev[prev.length - 1];
            // Only the still-active streaming turn we buffered for is a valid target.
            if (!last || last.role !== 'assistant' || !last.streaming || last.id !== buffered.turnId) {
                return prev;
            }
            return [
                ...prev.slice(0, -1),
                { ...last, streamingCodeTokens: mergeStreamEmits(last.streamingCodeTokens, buffered.emits) },
            ];
        });
    }, []);
    // Cell PTY coalescing. server_cell_pty_chunk can fire at terminal speed; the
    // sanitized lines are buffered per iteration and flushed into one setMessages per
    // animation frame (mirrors the streaming-AST buffer). The pending frame is
    // cancelled on unmount so no callback fires on a torn-down panel.
    const cellPtyBufferRef = useRef<Map<number, string[]> | null>(null);
    const cellPtyRafRef = useRef<number | null>(null);
    const flushCellPty = useCallback(() => {
        cellPtyRafRef.current = null;
        const buffered = cellPtyBufferRef.current;
        cellPtyBufferRef.current = null;
        if (!buffered || buffered.size === 0) { return; }
        setMessages(prev => {
            let next = prev;
            for (const [iter, lines] of buffered) {
                next = attachOrUpdateCellRun(next, iter, (it) => appendPtyLines(it, lines), nattName);
            }
            return next;
        });
    }, [nattName]);
    // Phase 7.11.3 — Stop-button optimistic flag (Zustand, transient).
    const isAborting    = useWorkspaceStore((s) => s.isAborting);
    const setIsAborting = useWorkspaceStore((s) => s.setIsAborting);
    // Phase 7.12 — in-flight Thought-Box resilience snapshot setter.
    const setInflightTurn = useWorkspaceStore((s) => s.setInflightTurn);
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Rich Plan side-panel (ADR-732). Held in TRANSIENT React state only — a
    // large plan is re-posted from host memory on remount, never written to the
    // webview's persistent state, so it can't exceed the setState quota.
    const [plan, setPlan] = useState<PlanDocumentShape | null>(null);

    // Stream-stall watchdog. `streamWatchdogMs` is the backend-governed budget
    // (posted via STREAM_WATCHDOG_MS after each submit); `lastStreamActivityRef`
    // is bumped on every streaming event so the interval can detect a lost
    // `server_stream_end` and finalize a hung turn.
    const [streamWatchdogMs, setStreamWatchdogMs] = useState<number>(DEFAULT_STREAM_WATCHDOG_MS);
    const lastStreamActivityRef = useRef<number>(0);

    // Natt — Phase 7.11.2: open/closed flag rehydrates from workspaceStore.
    // nattMessages stays sourced from the host transcript (7.9.B.20).
    const nattOpen    = useWorkspaceStore((s) => s.nattOpen);
    const setNattOpen = useWorkspaceStore((s) => s.setNattOpen);
    const [nattMessages, setNattMessages] = useState<NattMessage[]>(initial.initialNattMessages ?? []);
    const [hitlPending, setHitlPending] = useState<HITLIntervention | undefined>();

    // Telemetry
    const [wsStatus, setWsStatus] = useState<WsConnectionStatus>('disconnected');
    const [occStatus, setOccStatus] = useState<OccStatus>('clear');
    const [lockedFiles, setLockedFiles] = useState(0);
    const [telemetry, setTelemetry] = useState<TelemetryFrame | undefined>();
    const [snapshot, setSnapshot] = useState<TokenSnapshot | undefined>();
    const [indexing, setIndexing] = useState<IndexingState>({ state: 'idle' });

    // Core start menu — Phase 7.11.2: open/closed rehydrates from workspaceStore.
    const coreMenuOpen    = useWorkspaceStore((s) => s.coreMenuOpen);
    const setCoreMenuOpen = useWorkspaceStore((s) => s.setCoreMenuOpen);

    // Workspace folder (live-updated when VS Code opens/closes a folder)
    const [workspaceFolder, setWorkspaceFolder] = useState<string>(initial.workspaceFolder ?? '');

    // Attached context items (files/folders added via the main workspace picker)
    const [attachedItems, setAttachedItems] = useState<AttachedItem[]>([]);
    // Isolated Natt context items — never mixed with attachedItems
    const [nattAttachedItems, setNattAttachedItems] = useState<AttachedItem[]>([]);

    // Toasts
    const [toasts, setToasts] = useState<ToastItem[]>([]);
    const addToast = useCallback((level: ToastLevel, message: string) => {
        const id = ++_toastId;
        setToasts(prev => [...prev.slice(-2), { id, level, message }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 6000);
    }, []);

    const { recordChunk, tps } = useTpsCalculator();

    // Emit Natt visibility to the extension host (drives critical-notif gating).
    useEffect(() => {
        vscode.postMessage({ type: 'NATT_VISIBILITY', open: nattOpen });
    }, [nattOpen]);

    // Phase 7.9.B.20 — persist the per-session transcript so closing VS Code no
    // longer empties the session. Debounced; transient stream flags are stripped.
    // Phase 7.11.5 — explicitly drop `parserState` (large per-message object) so
    // it never reaches the host's workspaceState.
    useEffect(() => {
        const handle = setTimeout(() => {
            vscode.postMessage({
                type: 'PERSIST_TRANSCRIPT',
                // Phase 7.11.8 — carry checkpoint_id + is_abort_savepoint so
                // the rehydrated transcript still shows the ↪ Branch button.
                // Type predicate narrows to ConversationMessage[] so the destructure
                // of rich fields (steps, toolCalls, …) that don't exist on SystemMessage
                // is type-safe. System chips are transient display markers — not persisted.
                messages: messages
                    .filter((m): m is ConversationMessage => m.role !== 'system')
                    .map(({
                        id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                        checkpoint_id, is_abort_savepoint, authorLabel, liveTokens, checklist,
                    }) => ({
                        id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                        checkpoint_id, is_abort_savepoint, authorLabel, liveTokens, checklist,
                    })),
                nattMessages: nattMessages.map(({ id, role, content }) => ({ id, role, content })),
            });
        }, 400);
        return () => clearTimeout(handle);
    }, [messages, nattMessages]);

    // Phase 7.12 — in-flight Thought-Box resilience. Snapshot the active streaming
    // turn (id + content + thinking slice, NO parserState/toolCalls) into the
    // panel-survivable store, throttled. ADR-707 keeps reasoning display-only and
    // out of the host transcript, so this webview-local copy is the only way a
    // partial trace survives a teardown/reconnect. Cleared on server_stream_end.
    useEffect(() => {
        const inflight = messages.find((m): m is ConversationMessage => m.role === 'assistant' && !!(m as ConversationMessage).streaming);
        const handle = setTimeout(() => {
            setInflightTurn(inflight
                ? {
                    id: inflight.id,
                    role: inflight.role,
                    content: inflight.content,
                    streaming: true,
                    thinking: inflight.thinking,
                    thinkingTokens: inflight.thinkingTokens,
                    thinkingStartedAt: inflight.thinkingStartedAt,
                    thinkingElapsedMs: inflight.thinkingElapsedMs,
                    thinkingOpen: inflight.thinkingOpen,
                    steps: inflight.steps,
                    stepsDone: inflight.stepsDone,
                }
                : null);
        }, 200);
        return () => clearTimeout(handle);
    }, [messages, setInflightTurn]);

    // Phase 7.12 — on mount, rehydrate a persisted in-flight turn (survives a
    // panel teardown/reload). Merge by id so it never duplicates a turn already
    // present in the restored transcript. Runs once.
    useEffect(() => {
        const saved = useWorkspaceStore.getState().inflightTurn;
        if (saved?.id && saved.streaming) {
            setMessages(prev =>
                prev.some(m => m.id === saved.id) ? prev : [...prev, saved as Message]);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── WS / extension message handler ─────────────────────────
    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; payload?: unknown; config?: unknown; open?: boolean };

            // Any live stream event keeps the stall watchdog at bay.
            if (STREAM_ACTIVITY_EVENTS.has(msg.type)) {
                lastStreamActivityRef.current = performance.now();
            }

            switch (msg.type) {
                case 'WS_STATUS':
                    setWsStatus(msg.payload as WsConnectionStatus);
                    break;
                case 'STREAM_WATCHDOG_MS':
                    // Backend-governed stall timeout for the active model (zero-config).
                    if (typeof msg.payload === 'number' && msg.payload > 0) {
                        setStreamWatchdogMs(msg.payload);
                    }
                    break;
                case 'server_abort_ack': {
                    const d = msg.payload as { signalled?: boolean };
                    if (!d?.signalled) {
                        // No live task was cancelled (socket down, or it already
                        // finished). Clear the optimistic flag so Stop doesn't stay
                        // frozen, and surface the failure.
                        setIsAborting(false);
                        addToast('error', 'Stop failed — backend unreachable. The task may still be running.');
                    }
                    break;
                }
                case 'server_hitl_ack':
                    addToast('info', 'Decision received.');
                    break;
                case 'RENDER_DIFF': {
                    // Host-enriched inline diff: an approved edit was applied, and
                    // the host surfaced both sides. Attach to the assistant turn
                    // that explained the edit (or a fresh placeholder if the stream
                    // already ended). Built once on edit-arrival — never per token.
                    const d = msg.payload as { patch_id: string; files: Omit<DiffBlockShape, 'patch_id'>[] };
                    if (!d?.files?.length) { break; }
                    const incoming: DiffBlockShape[] = d.files.map(f => ({ ...f, patch_id: d.patch_id }));
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant') {
                            // New array + new message object so React.memo on DiffBlock
                            // only reconciles this turn — composer keystrokes that
                            // re-render Workspace leave every other turn's refs intact.
                            return [...prev.slice(0, -1), {
                                ...last,
                                diffBlocks: [...(last.diffBlocks ?? []), ...incoming],
                            }];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'assistant',
                            content: '',
                            diffBlocks: incoming,
                            authorLabel: authorLabelFor('assistant', nattName),
                        }];
                    });
                    break;
                }
                case 'REHYDRATE_TRANSCRIPT': {
                    // Phase 7.12 — host re-posts the authoritative per-session
                    // transcript when this hidden panel becomes visible again
                    // (retainContextWhenHidden:false destroys the webview, so the
                    // creation-time data-initial snapshot is stale). Merge by id —
                    // an in-flight streaming turn is preserved, never clobbered.
                    const rh = msg as unknown as {
                        messages?: Message[]; nattMessages?: NattMessage[];
                    };
                    if (Array.isArray(rh.messages)) {
                        // Normalize chips left spinning at the teardown that triggered
                        // this rehydrate so they don't resurrect as perpetual "pending".
                        setMessages(prev => normalizeStuckChips<Message>(mergeById(rh.messages as Message[], prev)));
                    }
                    if (Array.isArray(rh.nattMessages)) {
                        setNattMessages(prev => mergeById(rh.nattMessages as NattMessage[], prev));
                    }
                    // A transient Stop flag must never survive a hide→reveal teardown,
                    // or the button stays frozen on the remounted panel.
                    setIsAborting(false);
                    break;
                }
                case 'CONFIG_UPDATED':
                    setConfig((msg.config ?? null) as AilienantConfig | null);
                    break;
                case 'OPEN_NATT':
                    setNattOpen(true);
                    break;
                case 'server_token_chunk': {
                    const d = msg.payload as { token: string };
                    recordChunk();
                    setIsStreaming(true);
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            // Phase 7.11.5 — advance the markdown parser state
                            // ONCE per arriving token. O(1) per call.
                            const nextState = mdPushToken(
                                last.parserState ?? MD_INITIAL_STATE,
                                d.token,
                            );
                            // Phase 9 (ADR-707) — first answer token after a
                            // reasoning phase freezes the elapsed clock and
                            // auto-collapses the Thought Box.
                            const thinkingFreeze = freezeThinkingOnText(last, performance.now());
                            // Ghost Telemetry (ADR-723) — tally this answer token
                            // into the per-turn live counter (fold into the same
                            // immutable rebuild; no extra state / setState).
                            return [...prev.slice(0, -1), bumpLiveTokens({
                                ...last,
                                content: last.content + d.token,
                                parserState: nextState,
                                ...(thinkingFreeze ?? {}),
                            })];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'assistant',
                            content: d.token,
                            streaming: true,
                            parserState: mdPushToken(MD_INITIAL_STATE, d.token),
                            authorLabel: authorLabelFor('assistant', nattName),
                            liveTokens: 1,
                        }];
                    });
                    break;
                }
                case 'server_thinking_chunk': {
                    // Phase 9 (ADR-707) — accumulate raw reasoning into the
                    // streaming assistant turn's Thought Box. Display-only: this
                    // never touches `content` and is stripped before persist.
                    const d = msg.payload as { delta: string; token_count?: number };
                    recordChunk();
                    setIsStreaming(true);
                    setMessages(prev => {
                        const now = performance.now();
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            return [...prev.slice(0, -1),
                                accumulateThinking(last, d.delta, d.token_count, now)];
                        }
                        return [...prev, { id: mkId(), ...newThinkingTurn(d.delta, d.token_count, now), authorLabel: authorLabelFor('assistant', nattName) }];
                    });
                    break;
                }
                case 'server_pipeline_step': {
                    // Attach the node trace to the active assistant turn (Phase 7.9.B.14).
                    // Steps arrive before tokens, so create the turn placeholder if needed.
                    const d = msg.payload as { node_name?: string };
                    if (!d?.node_name) { break; }
                    const node = d.node_name;
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            return [...prev.slice(0, -1), { ...last, steps: [...(last.steps ?? []), node] }];
                        }
                        return [...prev, { id: mkId(), role: 'assistant', content: '', streaming: true, steps: [node], authorLabel: authorLabelFor('assistant', nattName) }];
                    });
                    break;
                }
                case 'server_plan_document': {
                    // The structured plan and its one-line chat pointer arrive in
                    // ONE message, so the docked panel and the conversation bubble
                    // update in a single render transition — no two-message race
                    // that could flash the pointer against an empty panel.
                    const doc = msg.payload as PlanDocumentShape;
                    setPlan(doc);
                    // Seed the in-chat execution checklist from the WBS. The early
                    // (seed-only) broadcast carries an empty summary; the per-step
                    // server_graph_mutation events flip the statuses from here.
                    if (doc.tasks && doc.tasks.length > 0) {
                        setMessages(prev => attachOrUpdateChecklist(prev, () => doc.tasks, nattName));
                    }
                    if (doc.summary) {
                        recordChunk();
                        setMessages(prev => {
                            const last = prev[prev.length - 1];
                            if (last?.role === 'assistant' && last.streaming) {
                                return [...prev.slice(0, -1), {
                                    ...last,
                                    content: last.content ? `${last.content}\n${doc.summary}` : doc.summary,
                                }];
                            }
                            // Idempotency: the host re-posts the latest plan when a
                            // hidden panel becomes visible again (to restore the docked
                            // panel after a webview teardown). The panel state is
                            // restored by setPlan above; re-appending the summary here
                            // would stack a duplicate chat pointer on every tab switch,
                            // so skip when a turn already carries this exact summary.
                            if (prev.some(m => m.role === 'assistant' && m.content === doc.summary)) {
                                return prev;
                            }
                            return [...prev, {
                                id: mkId(),
                                role: 'assistant',
                                content: doc.summary,
                                streaming: true,
                                authorLabel: authorLabelFor('assistant', nattName),
                            }];
                        });
                    }
                    break;
                }
                case 'server_graph_mutation': {
                    // A WBS step changed status (pending→completed/failed, etc.).
                    // Flip the matching checklist row by step_number on the most
                    // recent turn that carries a checklist — never create a turn.
                    const d = msg.payload as { step_number: number; new_status: string };
                    setMessages(prev => {
                        for (let i = prev.length - 1; i >= 0; i--) {
                            // System chips have no checklist; the cast is safe — only
                            // ConversationMessage rows ever carry checklist entries.
                            const m = prev[i] as ConversationMessage;
                            if (m.checklist && m.checklist.length > 0) {
                                const next = m.checklist.map((t: PlanWBSStep) =>
                                    t.step_number === d.step_number ? { ...t, status: d.new_status } : t);
                                return [...prev.slice(0, i), { ...m, checklist: next }, ...prev.slice(i + 1)];
                            }
                        }
                        return prev;
                    });
                    break;
                }
                case 'server_stream_end': {
                    setIsStreaming(false);
                    // Paint any tokens still buffered for this turn before it finalizes,
                    // so the block doesn't flash back to plain in the gap between
                    // stream-end and the CODE_TOKENS round-trip that supersedes the
                    // overlay. Runs while the turn is still streaming, so the flush
                    // guard accepts it; the finalize below preserves streamingCodeTokens.
                    if (streamTokenRafRef.current !== null) {
                        cancelAnimationFrame(streamTokenRafRef.current);
                        streamTokenRafRef.current = null;
                    }
                    flushStreamTokens();
                    // Disarm the stall watchdog — the stream ended cleanly.
                    lastStreamActivityRef.current = 0;
                    // Phase 7.11.3 — back to idle whether the stream ended
                    // naturally or because we aborted it.
                    setIsAborting(false);
                    // Phase 7.12 — the turn is complete and now lives in the host
                    // transcript; drop the in-flight resilience snapshot so a stale
                    // Thought Box can't resurrect on the next reload.
                    setInflightTurn(null);
                    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: capture the
                    // L2-promoted checkpoint_id (when present) and attach it
                    // to the last assistant turn so the per-message ↪ Branch
                    // button can target it. The payload field is optional
                    // (pre-7.11.8 servers don't emit it); absent → no button
                    // renders on that turn, no degradation elsewhere.
                    const _se = (msg.payload ?? {}) as { checkpoint_id?: string };
                    const _cid = typeof _se.checkpoint_id === 'string' ? _se.checkpoint_id : undefined;
                    setMessages(prev => prev.map((m, i) => {
                        // System chips carry no streaming state; skip them.
                        if (i !== prev.length - 1 || m.role === 'system') { return m; }
                        const cm = m as ConversationMessage;
                        return {
                            ...cm,
                            streaming: false,
                            stepsDone: true,
                            parserState: undefined,
                            checkpoint_id: _cid ?? cm.checkpoint_id,
                        };
                    }));
                    // Round-trip the just-finalized turn's code blocks to the host
                    // grammar engine. Content is already fully accumulated (the
                    // finalize above only flips flags), so read it from the mirror.
                    const finalized = messagesRef.current[messagesRef.current.length - 1];
                    if (finalized?.role === 'assistant' && finalized.id && finalized.content) {
                        requestCodeTokens(finalized.id, finalized.content);
                    }
                    break;
                }
                case 'CODE_TOKENS': {
                    // Host grammar engine answered the stream-end tokenize request.
                    // Merge the spans into the originating turn — but only if it
                    // still exists (cleared/replaced history drops the zombie reply,
                    // so an async reply for a gone turn is a safe no-op). Null
                    // results (unsupported lang / lexer fault) keep the plain fallback.
                    const d = msg.payload as {
                        turn_id?: string;
                        results?: { hash: string; ast_lines: ASTToken[][] | null }[];
                    };
                    if (!d?.turn_id || !Array.isArray(d.results) || d.results.length === 0) { break; }
                    setMessages(prev => {
                        const idx = prev.findIndex(m => m.id === d.turn_id);
                        if (idx < 0) { return prev; }   // zombie reply — turn is gone
                        // Code tokens are only ever attached to ConversationMessage turns.
                        const cm = prev[idx] as ConversationMessage;
                        const merged: Record<string, ASTToken[][]> = { ...(cm.codeTokens ?? {}) };
                        let changed = false;
                        for (const r of d.results!) {
                            if (r.ast_lines) { merged[r.hash] = r.ast_lines; changed = true; }
                        }
                        if (!changed) { return prev; }
                        return [
                            ...prev.slice(0, idx),
                            { ...cm, codeTokens: merged },
                            ...prev.slice(idx + 1),
                        ];
                    });
                    break;
                }
                case 'STREAM_CODE_TOKENS': {
                    // Host pushed a tokenized code line for the active streaming turn.
                    // Rather than reconcile per line, append to a per-turn buffer and
                    // coalesce all pushes that land in one animation frame into a single
                    // setMessages (see flushStreamTokens). Only the last streaming
                    // assistant turn is a valid target; a push with no streaming turn
                    // (e.g. arriving after stream-end) is dropped.
                    const st = msg.payload as { block_seq?: number; line_index?: number; ast?: ASTToken[] };
                    if (
                        typeof st?.block_seq !== 'number' ||
                        typeof st.line_index !== 'number' ||
                        !Array.isArray(st.ast)
                    ) { break; }
                    const last = messagesRef.current[messagesRef.current.length - 1];
                    if (!last || last.role !== 'assistant' || !last.streaming || !last.id) { break; }
                    // Stamp the buffer with this turn; a new turn starts a fresh batch.
                    let buf = streamTokenBufferRef.current;
                    if (!buf || buf.turnId !== last.id) {
                        buf = { turnId: last.id, emits: [] };
                        streamTokenBufferRef.current = buf;
                    }
                    buf.emits.push({
                        block_seq: st.block_seq, line_index: st.line_index, ast: st.ast,
                    });
                    if (streamTokenRafRef.current === null) {
                        streamTokenRafRef.current = requestAnimationFrame(flushStreamTokens);
                    }
                    break;
                }
                // ── Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips ──────
                // Each tool call broadcasts (start → stream_chunk*, → result)
                // and optionally a dep_graph attachment. We build the chip
                // up on the LAST assistant message (the same target the token
                // stream is appending to) so the artifact lives alongside the
                // narrative that introduced it.
                case 'server_tool_start': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        tool_name: string;
                        args: Record<string, unknown>;
                        side_effect_free?: boolean;
                    };
                    setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: d.tool_name,
                            args: d.args,
                            output_lines: [],
                            side_effect_free: d.side_effect_free,
                        }),
                        tool_name: d.tool_name,
                        args: d.args,
                        side_effect_free: d.side_effect_free,
                        status: 'pending',
                    }), nattName));
                    break;
                }
                case 'server_tool_stream_chunk': {
                    const d = msg.payload as { tool_call_id: string; chunk: string };
                    setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        // Hard tail-bound the retained output so a runaway tool
                        // cannot OOM the webview.
                        output_lines: [...(tc?.output_lines ?? []), d.chunk].slice(-MAX_TOOL_OUTPUT_LINES),
                    }), nattName));
                    break;
                }
                case 'server_tool_result': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        status: 'success' | 'error';
                        exit_code?: number;
                        duration_ms?: number;
                    };
                    setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        status: d.status,
                        exit_code: d.exit_code,
                        duration_ms: d.duration_ms,
                    }), nattName));
                    break;
                }
                case 'server_tool_dep_graph': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        nodes: { id: string; label: string }[];
                        edges: { from: string; to: string }[];
                    };
                    setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        dep_graph: { nodes: d.nodes, edges: d.edges },
                    }), nattName));
                    break;
                }
                case 'server_cell_tool_start': {
                    const d = msg.payload as {
                        iteration: number;
                        tool_name: string;
                        args_scrubbed: Record<string, string>;
                    };
                    setMessages(prev => attachOrUpdateCellRun(prev, d.iteration, it => ({
                        ...it,
                        tools: [...it.tools, { tool_name: d.tool_name, args_scrubbed: d.args_scrubbed }],
                    }), nattName));
                    break;
                }
                case 'server_cell_pty_chunk': {
                    const d = msg.payload as { iteration: number; text: string };
                    const cleaned = sanitizePtyChunk(d.text);
                    if (cleaned.length === 0) { break; }
                    // Split into lines, dropping the empty fragment a trailing newline
                    // produces. Buffer per iteration; the rAF flush appends them.
                    const lines = cleaned.split('\n');
                    if (lines.length > 0 && lines[lines.length - 1] === '') { lines.pop(); }
                    if (lines.length === 0) { break; }
                    const buf = cellPtyBufferRef.current ?? new Map<number, string[]>();
                    buf.set(d.iteration, [...(buf.get(d.iteration) ?? []), ...lines]);
                    cellPtyBufferRef.current = buf;
                    if (cellPtyRafRef.current === null) {
                        cellPtyRafRef.current = requestAnimationFrame(flushCellPty);
                    }
                    break;
                }
                case 'server_cell_ast_diff': {
                    const d = msg.payload as {
                        iteration: number;
                        path: string;
                        search: string;
                        replace: string;
                    };
                    setMessages(prev => attachOrUpdateCellRun(prev, d.iteration, it => ({
                        ...it,
                        diffs: [...it.diffs, { path: d.path, search: d.search, replace: d.replace }],
                    }), nattName));
                    break;
                }
                case 'server_cell_governor_tick': {
                    const d = msg.payload as {
                        iteration?: number;
                        step: number;
                        cost_usd: number;
                        elapsed_s: number;
                        axis: string | null;
                    };
                    // The governor tick keys off step (1-based); the iteration it
                    // belongs to is step - 1.
                    const iter = d.iteration ?? Math.max(0, d.step - 1);
                    setMessages(prev => attachOrUpdateCellRun(prev, iter, it => ({
                        ...it,
                        governor: { step: d.step, cost_usd: d.cost_usd, elapsed_s: d.elapsed_s, axis: d.axis },
                    }), nattName));
                    break;
                }
                case 'server_telemetry': {
                    const frame = msg.payload as TelemetryFrame;
                    setTelemetry(frame);
                    if (frame.is_red_alert && !telemetry?.is_red_alert) {
                        addToast('error', `Context insufficient (${frame.css_total.toFixed(0)}%) — inject more files`);
                    }
                    break;
                }
                case 'server_hitl_approval_request': {
                    const req = msg.payload as HITLIntervention & {
                        files?: DiffBlockShape[];
                        proposed_files?: Array<{ file_path: string; unified_diff?: string; new_content?: string; base_hash?: string | null }>;
                    };
                    // Soft-permission fast path: when the user enabled auto-accept
                    // and every risk metric is low (or none was attached), approve
                    // without ever mounting the card. Read the flag non-reactively
                    // so a toggle flipped after this handler was bound still counts;
                    // a stale closure value would silently ignore the user's choice.
                    // The conservative gate — ANY medium/high metric falls through
                    // to the manual card — is the whole safety contract here.
                    const autoAccept = useWorkspaceStore.getState().autoAcceptLowRisk;
                    const allLowRisk = (req.risk_metrics ?? []).every(m => m.level === 'low');
                    if (autoAccept && allLowRisk) {
                        vscode.postMessage({
                            type: 'HITL_RESPONSE',
                            approval_id: req.approval_id,
                            approved: true,
                        });
                        addToast('info', 'Auto-accepted low-risk edit');
                        break;
                    }
                    // The proposed diff rides in this same message (FILE_WRITE), so
                    // the inline diff and the Accept/Reject row mount together —
                    // attaching the blocks and setting hitlPending in one handler
                    // batches into a single React commit, so the buttons never
                    // appear without their code. The diff attaches to the awaiting
                    // assistant turn; patch_id = approval_id correlates them exactly.
                    //
                    // `files` is the host's syntax-highlighted both-sides preview.
                    // If it's absent (host preview faulted), fall back to the
                    // `proposed_files` that ride in this same payload and synthesize
                    // a diff from them — base_hash tells edit (existing) from create
                    // (new file). Either way the diff comes from ONE message, never a
                    // second broadcast, so the buttons can't mount without their code.
                    const blocks: DiffBlockShape[] = (req.files && req.files.length > 0)
                        ? req.files.map(f => ({ ...f, patch_id: req.approval_id }))
                        : (req.proposed_files ?? []).map(f => ({
                            patch_id: req.approval_id,
                            file_path: f.file_path,
                            old_content: '',
                            // Host preview faulted: the webview has no old side to
                            // reconstruct the O(Δ) diff against, so the new side may be
                            // empty here. The card still mounts; the user can authorize
                            // and the host's apply-time guard remains authoritative.
                            new_content: f.new_content ?? '',
                            status: (f.base_hash ? 'edit' : 'create') as 'edit' | 'create',
                        }));
                    if (blocks.length > 0) {
                        setMessages(prev => {
                            const last = prev[prev.length - 1];
                            if (last?.role === 'assistant') {
                                return [...prev.slice(0, -1), {
                                    ...last,
                                    diffBlocks: [...(last.diffBlocks ?? []), ...blocks],
                                }];
                            }
                            return [...prev, { id: mkId(), role: 'assistant', content: '', diffBlocks: blocks }];
                        });
                    }
                    setHitlPending(req);
                    addToast('warn', `${nattName} requires your authorization`);
                    break;
                }
                case 'server_natt_message': {
                    const d = msg.payload as { content: string; is_alert?: boolean };
                    setNattMessages(prev => [...prev, { id: mkId(), role: 'natt', content: d.content }]);
                    break;
                }
                case 'server_natt_token': {
                    // Phase 7.10.3 — accumulate batched analyst tokens into the active natt bubble.
                    // Phase 7.11.5 — also advance the markdown parser per token.
                    const d = msg.payload as { token: string };
                    setNattMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'natt' && last.streaming) {
                            const nextState = mdPushToken(
                                last.parserState ?? MD_INITIAL_STATE,
                                d.token,
                            );
                            return [...prev.slice(0, -1), {
                                ...last,
                                content: last.content + d.token,
                                parserState: nextState,
                            }];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'natt',
                            content: d.token,
                            streaming: true,
                            parserState: mdPushToken(MD_INITIAL_STATE, d.token),
                        }];
                    });
                    break;
                }
                case 'server_natt_stream_end':
                    // Phase 7.10.3 — finalize the streamed analyst bubble (context_version carried for 7.11 divergence).
                    // Phase 7.11.5 — drop parserState → stable renderer path.
                    setNattMessages(prev => prev.map((m, i) =>
                        i === prev.length - 1
                            ? { ...m, streaming: false, parserState: undefined }
                            : m));
                    break;
                case 'state_compacted': {
                    const d = msg.payload as { turns_compressed: number; compaction_message?: string };
                    // Use || (not ??) so an empty-string payload also triggers the fallback.
                    const label = d.compaction_message || `Context window compacted — ${d.turns_compressed} turn(s) summarized.`;
                    setMessages(prev => {
                        const chip: SystemMessage = { id: mkId(), role: 'system', content: label };
                        const last = prev[prev.length - 1];
                        // Insert BEFORE any streaming tail so server_token_chunk (which
                        // targets prev.last by role==='assistant'&&streaming) is not orphaned.
                        if ((last as ConversationMessage | undefined)?.streaming) {
                            return [...prev.slice(0, -1), chip, last];
                        }
                        return [...prev, chip];
                    });
                    break;
                }
                case 'server_indexing_started': {
                    const d = msg.payload as { total_files?: number };
                    setIndexing({ state: 'indexing', pct: 0, total_files: d?.total_files, files_indexed: 0 });
                    break;
                }
                case 'server_indexing_progress':
                case 'INDEXING_PROGRESS': {
                    // Backend wire format is { current, total, percentage } (websocket_manager.py).
                    // Completion is signalled as a 100% frame (current === total), so derive 'ready' here.
                    const d = msg.payload as { current?: number; total?: number; percentage?: number };
                    const filesIndexed = d?.current ?? 0;
                    const totalFiles = d?.total ?? 0;
                    const pct = d?.percentage ?? 0;
                    if (pct >= 100) {
                        // The completion signal arrives as a 1/1 frame after the real total frame;
                        // keep the larger count so it doesn't clobber the true node count.
                        const reported = totalFiles || filesIndexed;
                        setIndexing(prev => {
                            const prevCount = prev.state === 'ready' ? prev.node_count : 0;
                            return { state: 'ready', node_count: Math.max(prevCount, reported) };
                        });
                    } else {
                        setIndexing({
                            state: 'indexing',
                            pct,
                            files_indexed: filesIndexed,
                            total_files: totalFiles,
                        });
                    }
                    break;
                }
                case 'server_indexing_complete': {
                    const d = msg.payload as { node_count?: number };
                    setIndexing({ state: 'ready', node_count: d?.node_count ?? 0 });
                    break;
                }
                case 'server_indexing_error': {
                    const d = msg.payload as { reason?: string };
                    const reason = d?.reason ?? 'LLM configuration missing';
                    setIndexing({ state: 'error', reason });
                    addToast('error', reason);   // carries the exact actionable command (e.g. ollama pull …)
                    break;
                }
                case 'server_byom_config_applied': {
                    const d = msg.payload as { preset_id?: string; preset_name?: string };
                    addToast('info', `Preset "${d.preset_name ?? ''}" applied — retrying indexer…`);
                    setIndexing(prev => prev.state === 'error' ? { state: 'idle' } : prev);
                    break;
                }
                case 'server_model_warmup': {
                    const d = msg.payload as { model_name: string; is_local: boolean };
                    addToast('info', `Warming up ${d.model_name} (${d.is_local ? 'local' : 'cloud'})`);
                    break;
                }
                case 'server_oom_engaged': {
                    const d = msg.payload as { failed_model?: string; fallback_model?: string } | undefined;
                    const target = d?.fallback_model ? ` → ${d.fallback_model}` : '';
                    addToast('error', `OOM detected — falling back to cloud model${target}`);
                    break;
                }
                case 'OCC_CONFLICT':
                    setOccStatus('soft_conflict');
                    setLockedFiles(prev => prev + 1);
                    break;
                case 'OCC_CLEAR':
                    setOccStatus('clear');
                    setLockedFiles(0);
                    break;
                case 'TOKEN_SNAPSHOT':
                    // Preserve any context-occupancy fields already merged in by
                    // a prior CONTEXT_OCCUPANCY frame — the cost/savings snapshot
                    // and the window-occupancy read arrive on separate cadences.
                    setSnapshot(prev => ({ ...prev, ...(msg.payload as TokenSnapshot) }));
                    break;
                case 'CONTEXT_OCCUPANCY': {
                    const occ = msg.payload as { context_window: number; context_used_tokens: number };
                    setSnapshot(prev => ({
                        local_tokens: prev?.local_tokens ?? 0,
                        cloud_tokens: prev?.cloud_tokens ?? 0,
                        savings_pct: prev?.savings_pct ?? 0,
                        total_cost_usd: prev?.total_cost_usd ?? 0,
                        ...prev,
                        context_window: occ.context_window,
                        context_used_tokens: occ.context_used_tokens,
                    }));
                    break;
                }
                case 'TASK_STARTED': {
                    const d = msg.payload as { task_id: string };
                    setActiveTaskId(d.task_id);
                    break;
                }
                case 'PARALLEL_SESSION_NOTIFY': {
                    const count = (msg as unknown as { count: number }).count;
                    const label = count === 1 ? 'session is' : `${count} sessions are`;
                    addToast('info', `${count} parallel ${label} running — AILIENANT isolates each independently.`);
                    break;
                }
                case 'MENTION_NOTIFY': {
                    // @-mention expansion outcome (oversize folder skipped / cap
                    // hit) surfaced in-panel rather than as a native popup.
                    const m = msg as unknown as { level: ToastLevel; message: string };
                    addToast(m.level, m.message);
                    break;
                }
                // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel.
                case 'CHECKPOINTS_LIST': {
                    // Host fetched GET /api/v1/sessions/{id}/checkpoints and is
                    // handing us the chain. Open the picker overlay.
                    const entries = (msg.payload ?? []) as CheckpointEntry[];
                    setCheckpointPicker(Array.isArray(entries) ? entries : []);
                    break;
                }
                case 'SESSION_BRANCHED': {
                    // The host minted a new session and opened it in the
                    // sidebar; here we just dismiss any open picker and emit
                    // a confirmation toast. The webview's transcript was
                    // already replaced by the host's openSession() flow.
                    setCheckpointPicker(null);
                    addToast('info', '↪ Branched into a new session — original conversation preserved.');
                    break;
                }
                case 'BUDGET_UPDATED': {
                    const d = msg as unknown as { mode: BudgetLimitMode; weeklyUsd: number; monthlyUsd: number };
                    setBudgetLimitMode(d.mode);
                    setBudgetWeeklyUsd(d.weeklyUsd);
                    setBudgetMonthlyUsd(d.monthlyUsd);
                    break;
                }
                case 'WORKSPACE_UPDATED': {
                    const d = msg as unknown as { workspaceFolder: string };
                    setWorkspaceFolder(d.workspaceFolder);
                    break;
                }
                case 'CONVERSATION_CLEARED': {
                    setMessages([]);
                    setPlan(null);
                    break;
                }
                case 'PICKED_PATHS': {
                    const d = msg as unknown as { items: { path: string; kind: 'file' | 'directory' }[] };
                    const stamp = Date.now();
                    for (const item of d.items) {
                        vscode.postMessage({ type: 'ATTACH_CONTEXT', kind: item.kind, payload: item.path });
                    }
                    setAttachedItems(prev => [
                        ...prev,
                        ...d.items.map((item, i) => ({ id: `${stamp}-${i}`, path: item.path, kind: item.kind })),
                    ]);
                    break;
                }
                case 'PICKED_NATT_PATHS': {
                    const d = msg as unknown as { items: { path: string; kind: 'file' | 'directory' }[] };
                    const stamp = Date.now();
                    setNattAttachedItems(prev => [
                        ...prev,
                        ...d.items.map((item, i) => ({ id: `n-${stamp}-${i}`, path: item.path, kind: item.kind })),
                    ]);
                    break;
                }
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
    }, [addToast, recordChunk, telemetry, nattName, flushStreamTokens, flushCellPty]);

    // Phase 7.17.1 — cancel any pending coalesce frame on unmount so a queued flush
    // can't fire against a torn-down component.
    useEffect(() => () => {
        if (streamTokenRafRef.current !== null) {
            cancelAnimationFrame(streamTokenRafRef.current);
            streamTokenRafRef.current = null;
        }
        if (cellPtyRafRef.current !== null) {
            cancelAnimationFrame(cellPtyRafRef.current);
            cellPtyRafRef.current = null;
        }
        cellPtyBufferRef.current = null;
    }, []);

    useEffect(() => {
        // Instant while streaming — a smooth animation can't keep pace with a token
        // flood and visibly stutters; smooth is reserved for discrete new turns.
        messagesEndRef.current?.scrollIntoView({
            behavior: isStreaming ? 'auto' : 'smooth',
        });
    }, [messages, isStreaming]);

    // Stream-stall watchdog (ADR-715). While streaming, if no token / tool / natt
    // activity arrives within the backend-governed budget, `server_stream_end` was
    // almost certainly lost — finalize the turn so the UI never hangs on
    // "Streaming…". The budget is dictated by the backend (longer for slow local
    // engines), never a hardcoded product constant.
    useEffect(() => {
        if (!isStreaming) { return; }
        if (lastStreamActivityRef.current === 0) {
            lastStreamActivityRef.current = performance.now();
        }
        const interval = setInterval(() => {
            if (performance.now() - lastStreamActivityRef.current <= streamWatchdogMs) { return; }
            lastStreamActivityRef.current = 0;
            setIsStreaming(false);
            setIsAborting(false);
            setInflightTurn(null);
            setMessages(prev => prev.map((m, i) => {
                if (m.role === 'system') { return m; }
                const cm = m as ConversationMessage;
                if (i !== prev.length - 1 || !cm.streaming) { return m; }
                const calls = cm.toolCalls?.map((tc: ToolCallShape) =>
                    (tc.status === undefined || tc.status === 'pending')
                        ? { ...tc, status: 'error' as const }
                        : tc);
                return { ...cm, streaming: false, stepsDone: true, parserState: undefined, toolCalls: calls };
            }));
            addToast('warn', 'Stream stalled — no response from the backend. Ending this turn.');
        }, STREAM_WATCHDOG_TICK_MS);
        return () => clearInterval(interval);
    }, [isStreaming, streamWatchdogMs, addToast, setIsAborting, setInflightTurn]);

    // Submit a turn under an explicit execution mode. The mode is passed in
    // rather than read from the `mode` state so callers that flip the mode in
    // the same handler (plan acceptance) submit under the NEW mode immediately,
    // instead of racing React's asynchronous state update (which would resubmit
    // under the stale mode and re-deny the writes).
    const submitWithMode = useCallback((text: string, executionMode: ExecutionMode) => {
        const presetConfig = getPresetConfig(preset);
        setMessages(prev => [...prev, { id: mkId(), role: 'user', content: text, authorLabel: authorLabelFor('user', nattName) }]);
        const storeState = useWorkspaceStore.getState();
        vscode.postMessage({
            type: 'SUBMIT_TASK',
            value: text,
            preset,
            tier,
            execution_mode: executionMode,
            ...presetConfig,
            session_id: initial.sessionId,
            // Persisted Native Thinking toggle, read at submit time so the
            // latest value (survives reload) is injected.
            enable_native_thinking: storeState.nativeThinking,
            // Plan mode → route the turn into the backend Socratic loop.
            planner_mode_active: executionMode === 'plan_mode',
            // Explicit skill chip selected by the user (snake_case, undefined if none).
            invoked_skill_id: storeState.activeSkills?.[initial.sessionId]?.id ?? undefined,
        });
    }, [preset, tier, initial.sessionId]);

    const handleSubmit = useCallback((text: string) => {
        submitWithMode(text, mode);
    }, [submitWithMode, mode]);

    // A manual mode switch clears any plan left over from a prior turn so it can
    // never resurface as a stale acceptance panel (e.g. an Ask-turn plan popping
    // up the instant the user enters Plan mode). A plan re-appears only when a
    // fresh server_plan_document arrives while already in Plan mode.
    const handleModeChange = useCallback((next: ExecutionMode) => {
        setPlan(null);
        setMode(next);
    }, [setMode]);

    // Plan decision: accept and apply the plan with no further gating. The
    // agreement signal lets the backend's ideation loop synthesize and hand off
    // to the coder; submitting under Auto lifts the read-only plan gate so the
    // patches actually land on disk. Clearing the plan collapses the split panel
    // back to the chat composer as the agreement turn streams.
    const handlePlanAutoAccept = useCallback(() => {
        submitWithMode(AGREEMENT_SIGNAL, 'automatic');
        setMode('automatic');
        setPlan(null);
    }, [submitWithMode, setMode]);

    // Plan decision: accept the plan but route every file write through the
    // HITL approval card (Ask). Same agreement hand-off, stricter write gate.
    const handlePlanManualApprove = useCallback(() => {
        submitWithMode(AGREEMENT_SIGNAL, 'ask_before_edits');
        setMode('ask_before_edits');
        setPlan(null);
    }, [submitWithMode, setMode]);

    // Plan decision: reject the current plan and keep refining. With feedback,
    // it is a normal Socratic turn — stay in Plan mode so the analyst keeps the
    // read-only stance and the questioning loop continues. With no feedback, just
    // dismiss the acceptance panel back to the composer so the user can type the
    // next instruction (the plan panel must never trap the input).
    const handlePlanKeepPlanning = useCallback((feedback: string) => {
        const trimmed = feedback.trim();
        if (trimmed) {
            submitWithMode(trimmed, 'plan_mode');
        }
        setPlan(null);
    }, [submitWithMode]);

    // Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh.
    // ABORT_TASK keeps the client-side HTTP AbortController (legacy, harmless).
    // ABORT_MESH is the new path: workspace_panel.ts turns it into a
    // `client_abort_mesh` WS frame the backend resolves to Task.cancel().
    // The `isAborting` guard makes the second click idempotent.
    const handleAbort = useCallback(() => {
        if (isAborting) { return; }
        setIsAborting(true);
        vscode.postMessage({ type: 'ABORT_TASK' });
        vscode.postMessage({ type: 'ABORT_MESH' });
    }, [isAborting, setIsAborting]);

    // Interactive terminal: send a line of stdin to the live cell session and echo
    // it locally (the cell PTY runs echo-off, so the user otherwise sees nothing).
    const handleCellStdin = useCallback((iteration: number, line: string) => {
        vscode.postMessage({ type: 'PTY_STDIN', session_id: initial.sessionId, data: line + '\n' });
        setMessages(prev => attachOrUpdateCellRun(prev, iteration, it => appendPtyLines(it, [`› ${line}`]), nattName));
    }, [initial.sessionId, nattName]);

    // Phase 7.11.6 — Retry button on a Rich Tool Chip. The backend looks up
    // the historical ToolCallSpec and re-invokes verbatim; a new chip lands
    // alongside the old one (the original stays as record).
    const handleRetryTool = useCallback((toolCallId: string) => {
        vscode.postMessage({ type: 'RETRY_TOOL', tool_call_id: toolCallId });
    }, []);

    const handleDreamingToggle = useCallback((next: boolean, p: DreamingProfile) => {
        setDreamingActive(next);
        setDreamingProfile(p);
    }, []);

    const handleModelPrefChange = useCallback((id: string, m: OrchestrationMode) => {
        setActiveModelId(id);
        setOrchestrationMode(m);
        vscode.postMessage({ type: 'SET_MODEL_PREFERENCE', activeModelId: id, orchestrationMode: m });
    }, []);

    const handleNattSubmit = useCallback((text: string) => {
        setNattMessages(prev => [...prev, { id: mkId(), role: 'user', content: text }]);
        const contextPaths = nattAttachedItems.map(i => i.path);
        vscode.postMessage({
            type: 'NATT_MESSAGE',
            text,
            session_id: initial.sessionId,
            model_tier: useWorkspaceStore.getState().analystTier,
            ...(contextPaths.length > 0 && { context_paths: contextPaths }),
        });
        setNattAttachedItems([]);
    }, [initial.sessionId, nattAttachedItems]);

    const handleResolveHitl = useCallback((_id: string) => {
        setHitlPending(undefined);
    }, []);

    // ADR-724 — shared responder for the inline per-diff HITL row. Bound to the
    // currently-pending approval; the resolved-guard is reset whenever a new
    // approval arrives so a later request isn't swallowed by the previous one's
    // latched ref. Resolving here clears `hitlPending`, which simultaneously
    // tears down the Natt-pane card — one approval_id, one decision, no double-post.
    const hitlActiveApprovalId = hitlPending?.approval_id ?? '';
    const { respond: respondInlineHitl, resolvedRef: inlineHitlResolved } =
        useHitlResponder(hitlActiveApprovalId, handleResolveHitl);
    useEffect(() => {
        inlineHitlResolved.current = false;
    }, [hitlActiveApprovalId, inlineHitlResolved]);

    // "Request changes" — decline the pending edit AND re-submit the note as a
    // fresh turn so the agent re-proposes against the feedback. The reject carries
    // the comment so the backend acknowledges the hand-off ("Revising…") rather
    // than declaring the work discarded; the re-submit rides the same session id,
    // so the checkpointed thread carries prior context into the new proposal.
    const handleRequestChanges = useCallback((feedback: string) => {
        respondInlineHitl(false, { comment: feedback });
        submitWithMode(feedback, mode);
    }, [respondInlineHitl, submitWithMode, mode]);

    const handleBudgetChange = useCallback((
        mode: BudgetLimitMode, weeklyUsd: number, monthlyUsd: number,
    ) => {
        setBudgetLimitMode(mode);
        setBudgetWeeklyUsd(weeklyUsd);
        setBudgetMonthlyUsd(monthlyUsd);
        vscode.postMessage({ type: 'SET_BUDGET_LIMIT', mode, weeklyUsd, monthlyUsd });
    }, []);

    const rootAttrs: Record<string, string> = {
        'data-dreaming': dreamingActive ? 'true' : 'false',
        'data-tier':     tier,
        'data-alert':    telemetry?.is_red_alert ? 'true' : 'false',
        'data-hitl':     hitlPending ? 'true' : 'false',
        'data-natt':     nattOpen ? 'true' : 'false',
    };

    // A FILE_WRITE approval renders inline as the diff + action row in the main
    // chat (its blocks carry patch_id === approval_id). A diff-less approval
    // (e.g. budget overflow, degraded-sandbox exec) has no inline surface, so it
    // falls back to the authorization card — also in the main column, never the
    // analyst pane.
    const hitlHasDiff = !!hitlPending && messages.some(
        m => m.role !== 'system' && ((m as ConversationMessage).diffBlocks ?? []).some((db: DiffBlockShape) => db.patch_id === hitlPending.approval_id),
    );

    const wsLabel =
        wsStatus === 'connected' ? 'AILIENANT Core · Connected' :
        wsStatus === 'reconnecting' ? 'AILIENANT Core · Reconnecting…' :
        'AILIENANT Core · Offline';
    const wsTip =
        wsStatus === 'connected' ? 'Backend WebSocket online. Streaming, telemetry, and HITL are active.' :
        wsStatus === 'reconnecting' ? 'Reconnecting to the AILIENANT backend. Streaming is paused.' :
        'Backend WebSocket unreachable. Start the core (uvicorn main:app) on the configured port.';

    return (
        <Tooltip.Provider delayDuration={400} skipDelayDuration={150}>
            <div className="ws-root" {...rootAttrs}>
                <WorkspaceHeader
                    sessionTitle={initial.sessionTitle}
                    nattName={nattName}
                    nattOpen={nattOpen}
                    onToggleNatt={() => setNattOpen(!nattOpen)}
                    logoUri={initial.logoUri}
                />

                {/* Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel picker overlay.
                    Mounted only when the host hands us a checkpoint list
                    (CHECKPOINTS_LIST); the host fires the REST fetch when
                    the user picks `/context rewind` in the CommandPalette. */}
                {checkpointPicker !== null && (
                    <div className="ws-checkpoint-picker-overlay"
                         onClick={(e) => {
                             if (e.target === e.currentTarget) {
                                 setCheckpointPicker(null);
                             }
                         }}>
                        <CheckpointPicker
                            entries={checkpointPicker}
                            onCancel={() => setCheckpointPicker(null)}
                            onPick={(entry) => {
                                vscode.postMessage({
                                    type: 'BRANCH_FROM_CHECKPOINT',
                                    session_id: initial.sessionId,
                                    checkpoint_id: entry.checkpoint_id,
                                });
                                setCheckpointPicker(null);
                            }}
                        />
                    </div>
                )}

                {/* Status strip */}
                <div className="ws-status">
                    <Popover.Root open={coreMenuOpen} onOpenChange={setCoreMenuOpen} modal={false}>
                        <AiTooltip content={wsTip}>
                            <Popover.Trigger asChild>
                                <button className="ws-status-pill ws-status-pill--btn" aria-label="Backend options">
                                    <Icon name="network" size={12} />
                                    <span className="ws-status-dot" data-status={wsStatus} />
                                    <span>{wsLabel}</span>
                                </button>
                            </Popover.Trigger>
                        </AiTooltip>
                        <Popover.Portal>
                            <Popover.Content
                                className="ws-core-menu"
                                side="bottom"
                                align="center"
                                sideOffset={6}
                                collisionPadding={8}
                            >
                                <div className="ws-core-menu-head">AILIENANT Core</div>
                                <div className="ws-core-menu-actions">
                                    <button
                                        className="ws-core-menu-btn"
                                        onClick={() => {
                                            vscode.postMessage({ type: 'RESTART_BACKEND' });
                                            setCoreMenuOpen(false);
                                        }}
                                    >
                                        <Icon name="play" size={12} />
                                        Restart Core
                                    </button>
                                    <button
                                        className="ws-core-menu-btn"
                                        onClick={() => {
                                            vscode.postMessage({ type: 'OPEN_DASHBOARD' });
                                            setCoreMenuOpen(false);
                                        }}
                                    >
                                        <Icon name="external-link" size={12} />
                                        Open Dashboard
                                    </button>
                                </div>
                            </Popover.Content>
                        </Popover.Portal>
                    </Popover.Root>
                    {/* Workspace folder pill */}
                    {workspaceFolder ? (
                        <div className="ws-status-pill">
                            <Icon name="folder" size={12} />
                            <span>{workspaceFolder}</span>
                        </div>
                    ) : (
                        <Popover.Root modal={false}>
                            <Popover.Trigger asChild>
                                <button className="ws-status-pill ws-status-pill--btn ws-status-pill--warn" aria-label="Workspace options">
                                    <Icon name="folder" size={12} />
                                    <span>Awaiting Workspace</span>
                                </button>
                            </Popover.Trigger>
                            <Popover.Portal>
                                <Popover.Content className="ws-core-menu" side="bottom" align="center" sideOffset={6} collisionPadding={8}>
                                    <div className="ws-core-menu-head">Workspace Folder</div>
                                    <p className="ws-core-menu-desc">Open a folder so AILIENANT knows which project to work on.</p>
                                    <div className="ws-core-menu-actions">
                                        <button
                                            className="ws-core-menu-btn"
                                            onClick={() => vscode.postMessage({ type: 'OPEN_WORKSPACE' })}
                                        >
                                            <Icon name="folder" size={12} />
                                            Open Folder…
                                        </button>
                                    </div>
                                </Popover.Content>
                            </Popover.Portal>
                        </Popover.Root>
                    )}
                    <IndexingStatus state={indexing} />
                </div>

                {/* Main split-grid */}
                <main className={`ws-main${plan && mode === 'plan_mode' ? ' plan-mode-active' : ''}`}>
                    {/* LEFT: chat + bar */}
                    <section className="ws-main-left">
                        <CSSAlertBanner telemetry={telemetry} />

                        <div className="ws-messages">
                            {messages.length === 0 && (
                                <div className="ws-empty">
                                    <Icon name="sparkles" size={24} color="var(--accent-primary)" />
                                    <div>
                                        AILIENANT is ready.<br />
                                        Type a task or press <strong>/</strong> for the command palette.
                                    </div>
                                </div>
                            )}
                            {(() => {
                                return messages.map((m, i) => {
                                    // System notification chips (state_compacted) bypass the
                                    // full row structure — they render a plain string and have
                                    // no role header, tool chips, token footer, or ErrorBoundary.
                                    if (m.role === 'system') {
                                        return (
                                            <div key={m.id ?? `row-${i}`} className="ws-system-chip" role="status">
                                                {m.content}
                                            </div>
                                        );
                                    }
                                    return (
                                <ErrorBoundary
                                    key={m.id ?? `row-${i}`}
                                    label="message-row"
                                    resetKeys={[m.id, m.content, m.streaming]}
                                    fallback={<MessageRowFallback />}
                                >
                                    {m.role === 'assistant' && m.steps && m.steps.length > 0 && (
                                        <PipelineProgress steps={m.steps} done={!!m.stepsDone} />
                                    )}
                                    {/* Phase 9 (ADR-707) — Native Thinking Thought Box. */}
                                    {m.role === 'assistant' && m.thinking && (
                                        <ThoughtBox
                                            thinking={m.thinking}
                                            tokens={m.thinkingTokens ?? 0}
                                            startedAt={m.thinkingStartedAt}
                                            elapsedMs={m.thinkingElapsedMs}
                                            open={m.thinkingOpen ?? false}
                                            streaming={!!m.streaming}
                                            onToggle={() => setMessages(prev => prev.map((mm, j) =>
                                                j === i ? { ...(mm as ConversationMessage), thinkingOpen: !(mm as ConversationMessage).thinkingOpen } : mm))}
                                        />
                                    )}
                                    {/* Ghost Telemetry (ADR-723) — live action-log: the
                                        in-flight tool invocations, shown only while the turn
                                        streams. On stream-end the ToolChip stack below is the
                                        canonical record, so this while-you-wait view drops out. */}
                                    {m.role === 'assistant' && m.streaming && m.toolCalls && m.toolCalls.length > 0 && (
                                        <ActionLog toolCalls={m.toolCalls} />
                                    )}
                                    {/* Glass-box audit log for the autonomous agentic
                                        cell: per-iteration tool calls → terminal output →
                                        AST edits, with a budget-governor footer. */}
                                    {m.role === 'assistant' && m.cellRun && m.cellRun.iterations.length > 0 && (
                                        <CellAuditWidget run={m.cellRun} streaming={!!m.streaming} onStdin={handleCellStdin} />
                                    )}
                                    {/* Progressive execution checklist — the accepted
                                        WBS, its rows flipping ☐→✅ as steps complete. */}
                                    {m.role === 'assistant' && m.checklist && m.checklist.length > 0 && (
                                        <ExecutionChecklist tasks={m.checklist} />
                                    )}
                                    {(m.role === 'user' || m.content) && (
                                        <div
                                            className="ws-msg"
                                            data-role={m.role}
                                            data-streaming={m.streaming ? 'true' : 'false'}
                                        >
                                            <div className="ws-msg-role">
                                                {m.authorLabel ?? (m.role === 'user' ? 'You' : 'AILIENANT')}
                                            </div>
                                            <div className="ws-msg-content">
                                                {m.role === 'assistant' ? (
                                                    // Anti-flicker streaming markdown — parser state is cleared on stream end.
                                                    <MarkdownRenderer
                                                        content={m.content}
                                                        parserState={m.parserState}
                                                        streaming={!!m.streaming}
                                                        codeTokens={m.codeTokens}
                                                        streamingCodeTokens={m.streamingCodeTokens}
                                                    />
                                                ) : (
                                                    m.content
                                                )}
                                            </div>
                                        </div>
                                    )}
                                    {/* Ghost Telemetry (ADR-723) — per-message token footer.
                                        Ticks live while streaming (answer tokens tallied
                                        client-side + reasoning tokens) and freezes into a quiet
                                        per-turn total on stream-end. Distinct from the global
                                        FinOps HUD cost; zero-token turns render nothing. */}
                                    {m.role === 'assistant' && (() => {
                                        const total = (m.liveTokens ?? 0) + (m.thinkingTokens ?? 0);
                                        if (total === 0) { return null; }
                                        return (
                                            <div
                                                className="ws-turn-footer"
                                                data-streaming={m.streaming ? 'true' : 'false'}
                                                aria-live="off"
                                                aria-atomic="true"
                                            >
                                                {m.streaming
                                                    ? `Thinking… ${total} ${total === 1 ? 'token' : 'tokens'}`
                                                    : `${total} ${total === 1 ? 'token' : 'tokens'}`}
                                            </div>
                                        );
                                    })()}
                                    {/* Phase 7.11.6 — Rich Tool Chips attached to this turn. */}
                                    {m.role === 'assistant' && m.toolCalls && m.toolCalls.length > 0 && (
                                        <div className="ws-tool-chip-stack">
                                            {m.toolCalls.map(tc => (
                                                <ToolChip
                                                    key={tc.tool_call_id}
                                                    tc={tc}
                                                    onRetry={handleRetryTool}
                                                />
                                            ))}
                                        </div>
                                    )}
                                    {/* Inline Elite Diff Engine — split diffs for the edits in this turn.
                                        Approval is strictly sequential: a DiffBlock carries the inline
                                        Accept/Reject/Request-changes row and focused-diff keyboard ONLY
                                        while it is the file currently awaiting authorization, matched by
                                        patch_id === the live approval_id. Decided files stay as static
                                        diffs above the current pending one. */}
                                    {m.role === 'assistant' && m.diffBlocks && m.diffBlocks.length > 0 && (
                                        <div className="ws-diff-stack">
                                            {m.diffBlocks.map(db => {
                                                const blockHitlActive = !!hitlPending && db.patch_id === hitlPending.approval_id;
                                                return (
                                                    <DiffBlock
                                                        key={`${db.patch_id}:${db.file_path}`}
                                                        block={db}
                                                        hitlActive={blockHitlActive}
                                                        onRespond={blockHitlActive ? respondInlineHitl : undefined}
                                                        onRequestChanges={blockHitlActive ? handleRequestChanges : undefined}
                                                    />
                                                );
                                            })}
                                        </div>
                                    )}
                                    {/* Phase 7.11.8 (ADR-706 §4.5g) — per-message
                                        Time-Travel branch button. Only rendered
                                        on completed assistant turns that carry
                                        an L2-promoted checkpoint_id. */}
                                    {m.role === 'assistant' && !m.streaming && m.checkpoint_id && (
                                        <MessageActions
                                            checkpoint_id={m.checkpoint_id}
                                            session_id={initial.sessionId}
                                            message_index={i}
                                            is_abort_savepoint={m.is_abort_savepoint}
                                            post={vscode.postMessage.bind(vscode)}
                                        />
                                    )}
                                </ErrorBoundary>
                                    );
                                });
                            })()}
                            {/* Diff-less approval (budget / degraded exec) — the
                                authorization card lives in the main chat, not the
                                analyst pane. FILE_WRITE uses the inline diff row above. */}
                            {hitlPending && !hitlHasDiff && (
                                <HITLInterventionCard
                                    intervention={hitlPending}
                                    nattName={nattName}
                                    onResolved={handleResolveHitl}
                                />
                            )}
                            <div ref={messagesEndRef} />
                        </div>

                        {/* Attached context chips */}
                        {attachedItems.length > 0 && (
                            <div className="ws-attached-bar">
                                {attachedItems.map(item => {
                                    const label = item.path.split(/[/\\]/).pop() ?? item.path;
                                    return (
                                        <div key={item.id} className="ws-attached-chip" title={item.path}>
                                            <Icon name={item.kind === 'directory' ? 'folder' : 'file'} size={11} />
                                            <span>{label}</span>
                                            <button
                                                className="ws-attached-chip-remove"
                                                aria-label={`Remove ${label}`}
                                                onClick={() => setAttachedItems(prev => prev.filter(i => i.id !== item.id))}
                                            >
                                                <Icon name="x" size={10} />
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>
                        )}

                        {/* Composer + Telemetry sibling cards (matches manifest §7.2).
                            ONE composer in every mode — the HUD mode pill is the only
                            signal of Plan mode. The plan's "Accept" affordance lives on
                            the plan card, not on the input bar (Cursor/Claude-Code shape). */}
                        <div className="ws-bottom">
                            <PromptBar
                                disabled={Boolean(hitlPending)}
                                placeholder={
                                    hitlPending
                                        ? `${nattName} is waiting for your decision`
                                        : mode === 'plan_mode'
                                            ? `Describe what you want to build — ${nattName} will grill you into a plan…`
                                            : undefined
                                }
                                activeTaskId={activeTaskId}
                                isStreaming={isStreaming}
                                isAborting={isAborting}
                                config={config}
                                mode={mode}
                                preset={preset}
                                onModeChange={handleModeChange}
                                onPresetChange={setPreset}
                                dreamingActive={dreamingActive}
                                dreamingProfile={dreamingProfile}
                                onDreamingToggle={handleDreamingToggle}
                                activeModelId={activeModelId}
                                orchestrationMode={orchestrationMode}
                                onModelPrefChange={handleModelPrefChange}
                                sessionId={initial.sessionId}
                                onSubmit={handleSubmit}
                                onAbort={handleAbort}
                            />
                            <TelemetryHUD
                                occStatus={occStatus}
                                lockedFiles={lockedFiles}
                                tps={tps}
                                snapshot={snapshot}
                                budgetUsd={budgetUsd}
                                telemetry={telemetry}
                                budgetLimitMode={budgetLimitMode}
                                budgetWeeklyUsd={budgetWeeklyUsd}
                                budgetMonthlyUsd={budgetMonthlyUsd}
                                onBudgetChange={handleBudgetChange}
                            />
                        </div>
                    </section>

                    {/* RIGHT: Natt pane */}
                    {nattOpen && (
                        <NattCanvas
                            nattName={nattName}
                            messages={nattMessages}
                            disabled={Boolean(hitlPending)}
                            nattAttachedItems={nattAttachedItems}
                            onNattRemoveAttached={(id) => setNattAttachedItems(prev => prev.filter(i => i.id !== id))}
                            onClose={() => setNattOpen(false)}
                            onSendMessage={handleNattSubmit}
                        />
                    )}

                    {/* RIGHT: dedicated plan-acceptance surface. In Plan mode the
                        plan opens as a split panel carrying the three-way decision
                        (auto-accept / manual-approve / keep-planning); the chat
                        composer folds away so the acceptance controls own the input. */}
                    {plan && mode === 'plan_mode' && (
                        <aside className="plan-panel-container">
                            <PlanAcceptancePanel
                                plan={plan}
                                onAutoAccept={handlePlanAutoAccept}
                                onManualApprove={handlePlanManualApprove}
                                onKeepPlanning={handlePlanKeepPlanning}
                                isStreaming={isStreaming}
                            />
                        </aside>
                    )}
                </main>

                {/* Toasts */}
                <div className="ws-toast-stack">
                    {toasts.map(t => (
                        <div key={t.id} className="ws-toast" data-level={t.level} role="alert">
                            {t.message}
                        </div>
                    ))}
                </div>
            </div>
        </Tooltip.Provider>
    );
}
