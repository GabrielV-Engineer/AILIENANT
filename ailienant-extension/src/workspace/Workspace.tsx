import { Fragment, useState, useCallback, useEffect, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import * as Popover from '@radix-ui/react-popover';
import { vscode } from './vscode_bridge';
import { useWorkspaceStore } from './workspaceStore';
import type { WorkspaceSurface } from './workspaceStore';
import {
    BudgetLimitMode, ReasoningPreset, DreamingProfile,
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot, OrchestrationMode,
    ToolCallShape, DiffBlockShape,
} from '../shared/config';
import type { AilienantConfig, ExecutionMode, IndexingState } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { Icon } from '../shared/Icon';
import { Tooltip as AiTooltip } from '../shared/Tooltip';
import { WorkspaceHeader } from './components/WorkspaceHeader';
import { TelemetryHUD, useTpsCalculator } from './components/TelemetryHUD';
import { CSSAlertBanner } from './components/CSSAlertBanner';
import { PromptBar } from './components/PromptBar';
import { PlannerSession } from './components/PlannerSession';
import { NattCanvas } from './components/NattCanvas';
import { MarkdownRenderer } from './components/MarkdownRenderer';
import { ThoughtBox } from './components/ThoughtBox';
import { accumulateThinking, newThinkingTurn, freezeThinkingOnText } from './utils/thinkingReducer';
import { ToolChip } from './components/ToolChip';
import { DiffBlock } from './components/DiffBlock';
import { MessageActions } from './components/MessageActions';
import { CheckpointPicker, type CheckpointEntry } from './components/CheckpointPicker';
import {
    INITIAL_STATE as MD_INITIAL_STATE,
    pushToken as mdPushToken,
    type ParserState as MdParserState,
} from './utils/StreamingMarkdownParser';
import { IndexingStatus } from './components/IndexingStatus';
import { PipelineProgress } from './components/PipelineProgress';
import type { HITLIntervention } from './components/HITLInterventionCard';
import { getPresetConfig } from './hooks/useReasoningPreset';

type ToastLevel = 'info' | 'warn' | 'error';
interface ToastItem { id: number; level: ToastLevel; message: string; }
let _toastId = 0;

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
]);

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

export interface Message {
    // Phase 7.12 — stable per-turn id (client-minted via crypto.randomUUID at
    // creation). Display-layer only: it keys the REHYDRATE_TRANSCRIPT merge so a
    // tab re-reveal never clobbers a live in-flight turn. Never sent to the core.
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    steps?: string[];      // pipeline node trace for this assistant turn (Phase 7.9.B.14)
    stepsDone?: boolean;   // true after server_stream_end → ✓ + auto-collapse
    // Phase 7.11.5 (ADR-706 §4.5e) — incremental markdown parser state. Live
    // only while streaming; cleared on `server_stream_end` so the renderer's
    // stable fast path takes over. Transient — explicitly stripped before
    // PERSIST_TRANSCRIPT (see destructure below).
    parserState?: MdParserState;
    // Phase 7.11.6 (ADR-706 §4.5f) — tool-chip artifacts attached to this turn.
    // Each entry is built incrementally from server_tool_start, _stream_chunk
    // and _result events keyed by tool_call_id.
    toolCalls?: ToolCallShape[];
    // Inline diffs for edits applied during this turn — one entry per file,
    // surfaced by the host (RENDER_DIFF) after PatchActuator applies. Attached to
    // the turn that explained the edit and persisted so a teardown mid-render
    // re-hydrates the diff. Display-only; never sent to the core.
    diffBlocks?: DiffBlockShape[];
    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: the L2-promoted checkpoint
    // that wraps this completed assistant turn. Populated from
    // `server_stream_end`. Only assistant messages carry one; absent on user
    // messages and on streaming-in-flight turns. Survives PERSIST_TRANSCRIPT
    // so the branch button still works on rehydrated sessions.
    checkpoint_id?: string;
    // Phase 7.11.8 — flips the branch button's icon to ⏹ when this turn
    // ended in a user_abort emergency savepoint (Phase 7.11.3).
    is_abort_savepoint?: boolean;
    // Frozen at ingestion by authorLabelFor() — the row component is pure and
    // never reads reactive config. Legacy turns without this field fall back to
    // a static literal at render time; never retroactively relabeled.
    authorLabel?: string;
    // Phase 9 (ADR-707) — Native Thinking. Raw reasoning accumulated for this
    // turn (display-only — NEVER persisted to the transcript or fed back to the
    // agent). `thinkingStartedAt`/`thinkingElapsedMs` drive the chronometrics:
    // start is stamped on the first reasoning delta; elapsed is frozen when the
    // first answer (text) delta arrives. `thinkingOpen` controls the accordion
    // (auto-expands while reasoning, auto-collapses when the answer begins).
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
}
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
    // The interaction surface is derived from the execution mode — the HUD is the
    // single source of truth. Plan mode owns the Socratic Planner; everything else
    // is the standard chat composer.
    const surface: WorkspaceSurface = mode === 'plan_mode' ? 'planner' : 'chat';

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
    const [isStreaming, setIsStreaming] = useState(false);
    // Phase 7.11.3 — Stop-button optimistic flag (Zustand, transient).
    const isAborting    = useWorkspaceStore((s) => s.isAborting);
    const setIsAborting = useWorkspaceStore((s) => s.setIsAborting);
    // Phase 7.12 — in-flight Thought-Box resilience snapshot setter.
    const setInflightTurn = useWorkspaceStore((s) => s.setInflightTurn);
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

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
                messages: messages.map(({
                    id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                    checkpoint_id, is_abort_savepoint, authorLabel,
                }) => ({
                    id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                    checkpoint_id, is_abort_savepoint, authorLabel,
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
        const inflight = messages.find(m => m.streaming && m.role === 'assistant');
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
                        setMessages(prev => normalizeStuckChips(mergeById(rh.messages as Message[], prev)));
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
                            return [...prev.slice(0, -1), {
                                ...last,
                                content: last.content + d.token,
                                parserState: nextState,
                                ...(thinkingFreeze ?? {}),
                            }];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'assistant',
                            content: d.token,
                            streaming: true,
                            parserState: mdPushToken(MD_INITIAL_STATE, d.token),
                            authorLabel: authorLabelFor('assistant', nattName),
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
                case 'server_stream_end': {
                    setIsStreaming(false);
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
                    setMessages(prev => prev.map((m, i) =>
                        i === prev.length - 1
                            // Phase 7.11.5 — drop parserState so MarkdownRenderer
                            // takes its stable single-pass render path.
                            ? {
                                ...m,
                                streaming: false,
                                stepsDone: true,
                                parserState: undefined,
                                checkpoint_id: _cid ?? m.checkpoint_id,
                            }
                            : m));
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
                case 'server_telemetry': {
                    const frame = msg.payload as TelemetryFrame;
                    setTelemetry(frame);
                    if (frame.is_red_alert && !telemetry?.is_red_alert) {
                        addToast('error', `Context insufficient (${frame.css_total.toFixed(0)}%) — inject more files`);
                    }
                    break;
                }
                case 'server_hitl_approval_request': {
                    const req = msg.payload as HITLIntervention;
                    setHitlPending(req);
                    setNattOpen(true);
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
                    setSnapshot(msg.payload as TokenSnapshot);
                    break;
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
    }, [addToast, recordChunk, telemetry, nattName]);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

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
                if (i !== prev.length - 1 || !m.streaming) { return m; }
                const calls = m.toolCalls?.map(tc =>
                    (tc.status === undefined || tc.status === 'pending')
                        ? { ...tc, status: 'error' as const }
                        : tc);
                return { ...m, streaming: false, stepsDone: true, parserState: undefined, toolCalls: calls };
            }));
            addToast('warn', 'Stream stalled — no response from the backend. Ending this turn.');
        }, STREAM_WATCHDOG_TICK_MS);
        return () => clearInterval(interval);
    }, [isStreaming, streamWatchdogMs, addToast, setIsAborting, setInflightTurn]);

    const handleSubmit = useCallback((text: string) => {
        const presetConfig = getPresetConfig(preset);
        setMessages(prev => [...prev, { id: mkId(), role: 'user', content: text, authorLabel: authorLabelFor('user', nattName) }]);
        vscode.postMessage({
            type: 'SUBMIT_TASK',
            value: text,
            preset,
            tier,
            execution_mode: mode,
            ...presetConfig,
            session_id: initial.sessionId,
            // Phase 9 (ADR-707) — persisted Native Thinking toggle, read at
            // submit time so the latest value (survives reload) is injected.
            enable_native_thinking: useWorkspaceStore.getState().nativeThinking,
            // Plan mode → route the turn into the backend Socratic loop.
            planner_mode_active: mode === 'plan_mode',
        });
    }, [preset, tier, mode, initial.sessionId]);

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
            ...(contextPaths.length > 0 && { context_paths: contextPaths }),
        });
        setNattAttachedItems([]);
    }, [initial.sessionId, nattAttachedItems]);

    const handleResolveHitl = useCallback((_id: string) => {
        setHitlPending(undefined);
    }, []);

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
                <main className="ws-main">
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
                            {messages.map((m, i) => (
                                <Fragment key={i}>
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
                                                j === i ? { ...mm, thinkingOpen: !mm.thinkingOpen } : mm))}
                                        />
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
                                                    />
                                                ) : (
                                                    m.content
                                                )}
                                            </div>
                                        </div>
                                    )}
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
                                    {/* Inline Elite Diff Engine — split diffs for edits applied during this turn. */}
                                    {m.role === 'assistant' && m.diffBlocks && m.diffBlocks.length > 0 && (
                                        <div className="ws-diff-stack">
                                            {m.diffBlocks.map(db => (
                                                <DiffBlock
                                                    key={`${db.patch_id}:${db.file_path}`}
                                                    block={db}
                                                />
                                            ))}
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
                                </Fragment>
                            ))}
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
                            The active surface is derived from the HUD execution mode —
                            Plan mode owns the Planner; there is no separate surface toggle. */}
                        <div className="ws-bottom">
                            {surface === 'planner' ? (
                                <PlannerSession
                                    disabled={Boolean(hitlPending)}
                                    isStreaming={isStreaming}
                                    isAborting={isAborting}
                                    canAgree={!isStreaming && messages.some(m => m.role === 'assistant')}
                                    nattName={nattName}
                                    onSubmit={handleSubmit}
                                    onAbort={handleAbort}
                                    onExit={() => setMode('automatic')}
                                />
                            ) : (
                                <PromptBar
                                    disabled={Boolean(hitlPending)}
                                    placeholder={hitlPending ? `${nattName} is waiting for your decision` : undefined}
                                    activeTaskId={activeTaskId}
                                    isStreaming={isStreaming}
                                    isAborting={isAborting}
                                    config={config}
                                    mode={mode}
                                    preset={preset}
                                    onModeChange={setMode}
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
                            )}
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
                            pendingIntervention={hitlPending}
                            disabled={Boolean(hitlPending)}
                            nattAttachedItems={nattAttachedItems}
                            onNattRemoveAttached={(id) => setNattAttachedItems(prev => prev.filter(i => i.id !== id))}
                            onClose={() => setNattOpen(false)}
                            onResolveIntervention={handleResolveHitl}
                            onSendMessage={handleNattSubmit}
                        />
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
