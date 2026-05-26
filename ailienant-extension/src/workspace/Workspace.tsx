import { Fragment, useState, useCallback, useEffect, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import * as Popover from '@radix-ui/react-popover';
import { vscode } from './vscode_bridge';
import { useWorkspaceStore } from './workspaceStore';
import {
    BudgetLimitMode, ReasoningPreset, InferenceTier, DreamingProfile,
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot, OrchestrationMode,
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
import { IndexingStatus } from './components/IndexingStatus';
import { PipelineProgress } from './components/PipelineProgress';
import type { HITLIntervention } from './components/HITLInterventionCard';
import { getPresetConfig } from './hooks/useReasoningPreset';

type ToastLevel = 'info' | 'warn' | 'error';
interface ToastItem { id: number; level: ToastLevel; message: string; }
let _toastId = 0;

export interface Message {
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    steps?: string[];      // pipeline node trace for this assistant turn (Phase 7.9.B.14)
    stepsDone?: boolean;   // true after server_stream_end → ✓ + auto-collapse
}
export interface NattMessage { role: 'natt' | 'user'; content: string; streaming?: boolean; }
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

    // Mode / preset / tier (all live inside ModeMenu) — Phase 7.11.2: persisted
    // panel-lifetime via workspaceStore (rehydrates on tab-switch).
    const mode    = useWorkspaceStore((s) => s.mode);
    const setMode = useWorkspaceStore((s) => s.setMode);
    const preset    = useWorkspaceStore((s) => s.preset);
    const setPreset = useWorkspaceStore((s) => s.setPreset);
    const tier    = useWorkspaceStore((s) => s.tier);
    const setTier = useWorkspaceStore((s) => s.setTier);

    // Dreaming
    const [dreamingActive, setDreamingActive] = useState(false);
    const [dreamingProfile, setDreamingProfile] = useState<DreamingProfile>('Hybrid');

    // Models menu preferences (persisted via SET_MODEL_PREFERENCE)
    const [activeModelId, setActiveModelId] = useState<string>(initial.activeModelId ?? '');
    const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(initial.orchestrationMode ?? 'auto');

    // Budget — global persistent setting, not per-session
    const [budgetLimitMode,  setBudgetLimitMode]  = useState<BudgetLimitMode>(initial.budgetLimitMode);
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
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

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
    useEffect(() => {
        const handle = setTimeout(() => {
            vscode.postMessage({
                type: 'PERSIST_TRANSCRIPT',
                messages: messages.map(({ role, content, steps, stepsDone }) => ({
                    role, content, steps, stepsDone,
                })),
                nattMessages,
            });
        }, 400);
        return () => clearTimeout(handle);
    }, [messages, nattMessages]);

    // ── WS / extension message handler ─────────────────────────
    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; payload?: unknown; config?: unknown; open?: boolean };

            switch (msg.type) {
                case 'WS_STATUS':
                    setWsStatus(msg.payload as WsConnectionStatus);
                    break;
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
                            return [...prev.slice(0, -1), { ...last, content: last.content + d.token }];
                        }
                        return [...prev, { role: 'assistant', content: d.token, streaming: true }];
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
                        return [...prev, { role: 'assistant', content: '', streaming: true, steps: [node] }];
                    });
                    break;
                }
                case 'server_stream_end':
                    setIsStreaming(false);
                    // Phase 7.11.3 — back to idle whether the stream ended
                    // naturally or because we aborted it.
                    setIsAborting(false);
                    setMessages(prev => prev.map((m, i) =>
                        i === prev.length - 1 ? { ...m, streaming: false, stepsDone: true } : m));
                    break;
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
                    setNattMessages(prev => [...prev, { role: 'natt', content: d.content }]);
                    break;
                }
                case 'server_natt_token': {
                    // Phase 7.10.3 — accumulate batched analyst tokens into the active natt bubble.
                    const d = msg.payload as { token: string };
                    setNattMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'natt' && last.streaming) {
                            return [...prev.slice(0, -1), { ...last, content: last.content + d.token }];
                        }
                        return [...prev, { role: 'natt', content: d.token, streaming: true }];
                    });
                    break;
                }
                case 'server_natt_stream_end':
                    // Phase 7.10.3 — finalize the streamed analyst bubble (context_version carried for 7.11 divergence).
                    setNattMessages(prev => prev.map((m, i) =>
                        i === prev.length - 1 ? { ...m, streaming: false } : m));
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
                case 'OOM_ENGAGED':
                    addToast('error', 'OOM detected — falling back to cloud model');
                    break;
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

    const handleSubmit = useCallback((text: string) => {
        const presetConfig = getPresetConfig(preset);
        setMessages(prev => [...prev, { role: 'user', content: text }]);
        vscode.postMessage({
            type: 'SUBMIT_TASK',
            value: text,
            preset,
            tier,
            execution_mode: mode,
            ...presetConfig,
            session_id: initial.sessionId,
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
        setNattMessages(prev => [...prev, { role: 'user', content: text }]);
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
                                    {(m.role === 'user' || m.content) && (
                                        <div
                                            className="ws-msg"
                                            data-role={m.role}
                                            data-streaming={m.streaming ? 'true' : 'false'}
                                        >
                                            {m.content}
                                        </div>
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

                        {/* PromptBar + Telemetry sibling cards (matches manifest §7.2) */}
                        <div className="ws-bottom">
                            <PromptBar
                                disabled={Boolean(hitlPending)}
                                placeholder={hitlPending ? `${nattName} is waiting for your decision` : undefined}
                                activeTaskId={activeTaskId}
                                isStreaming={isStreaming}
                                isAborting={isAborting}
                                config={config}
                                mode={mode}
                                preset={preset}
                                tier={tier}
                                onModeChange={setMode}
                                onPresetChange={setPreset}
                                onTierChange={setTier}
                                dreamingActive={dreamingActive}
                                dreamingProfile={dreamingProfile}
                                onDreamingToggle={handleDreamingToggle}
                                activeModelId={activeModelId}
                                orchestrationMode={orchestrationMode}
                                onModelPrefChange={handleModelPrefChange}
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
