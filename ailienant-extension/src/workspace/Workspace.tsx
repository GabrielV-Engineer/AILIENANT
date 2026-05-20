import { useState, useCallback, useEffect, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { vscode } from './vscode_bridge';
import {
    ReasoningPreset, InferenceTier, DreamingProfile,
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot,
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
import type { HITLIntervention } from './components/HITLInterventionCard';
import { getPresetConfig } from './hooks/useReasoningPreset';

type ToastLevel = 'info' | 'warn' | 'error';
interface ToastItem { id: number; level: ToastLevel; message: string; }
let _toastId = 0;

interface Message { role: 'user' | 'assistant'; content: string; streaming?: boolean; }
interface NattMessage { role: 'natt' | 'user'; content: string; }

interface InitialState {
    sessionId: string;
    sessionTitle: string;
    config: AilienantConfig | null;
    logoUri: string;
}

export function Workspace({ initial }: { initial: InitialState }): JSX.Element {
    const [config, setConfig] = useState<AilienantConfig | null>(initial.config);
    const nattName = config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;

    // Mode / preset / tier (all live inside ModeMenu)
    const [mode, setMode] = useState<ExecutionMode>('automatic');
    const [preset, setPreset] = useState<ReasoningPreset>('architect');
    const [tier, setTier] = useState<InferenceTier>('HYBRID');

    // Dreaming
    const [dreamingActive, setDreamingActive] = useState(false);
    const [dreamingProfile, setDreamingProfile] = useState<DreamingProfile>('Hybrid');

    // Budget
    const [budgetUsd] = useState(config?.finops?.budget_usd ?? 0);

    // Chat
    const [messages, setMessages] = useState<Message[]>([]);
    const [isStreaming, setIsStreaming] = useState(false);
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Natt
    const [nattOpen, setNattOpen] = useState(false);
    const [nattMessages, setNattMessages] = useState<NattMessage[]>([]);
    const [hitlPending, setHitlPending] = useState<HITLIntervention | undefined>();

    // Telemetry
    const [wsStatus, setWsStatus] = useState<WsConnectionStatus>('disconnected');
    const [occStatus, setOccStatus] = useState<OccStatus>('clear');
    const [lockedFiles, setLockedFiles] = useState(0);
    const [telemetry, setTelemetry] = useState<TelemetryFrame | undefined>();
    const [snapshot, setSnapshot] = useState<TokenSnapshot | undefined>();
    const [indexing, setIndexing] = useState<IndexingState>({ state: 'idle' });

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
                case 'server_stream_end':
                    setIsStreaming(false);
                    setMessages(prev => prev.map((m, i) => i === prev.length - 1 ? { ...m, streaming: false } : m));
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
                case 'server_indexing_started': {
                    const d = msg.payload as { total_files?: number };
                    setIndexing({ state: 'indexing', pct: 0, total_files: d?.total_files, files_indexed: 0 });
                    break;
                }
                case 'server_indexing_progress':
                case 'INDEXING_PROGRESS': {
                    const d = msg.payload as { pct?: number; files_indexed?: number; total_files?: number };
                    setIndexing({
                        state: 'indexing',
                        pct: d?.pct ?? 0,
                        files_indexed: d?.files_indexed,
                        total_files: d?.total_files,
                    });
                    break;
                }
                case 'server_indexing_complete': {
                    const d = msg.payload as { node_count?: number };
                    setIndexing({ state: 'ready', node_count: d?.node_count ?? 0 });
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

    const handleAbort = useCallback(() => {
        vscode.postMessage({ type: 'ABORT_TASK' });
    }, []);

    const handleDreamingToggle = useCallback((next: boolean, p: DreamingProfile) => {
        setDreamingActive(next);
        setDreamingProfile(p);
    }, []);

    const handleNattSubmit = useCallback((text: string) => {
        setNattMessages(prev => [...prev, { role: 'user', content: text }]);
        vscode.postMessage({ type: 'NATT_MESSAGE', text, session_id: initial.sessionId });
    }, [initial.sessionId]);

    const handleResolveHitl = useCallback((_id: string) => {
        setHitlPending(undefined);
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
                    onToggleNatt={() => setNattOpen(o => !o)}
                    logoUri={initial.logoUri}
                />

                {/* Status strip */}
                <div className="ws-status">
                    <AiTooltip content={wsTip}>
                        <div className="ws-status-pill">
                            <Icon name="network" size={12} />
                            <span className="ws-status-dot" data-status={wsStatus} />
                            <span>{wsLabel}</span>
                        </div>
                    </AiTooltip>
                    <span className="ws-status-divider" />
                    <IndexingStatus state={indexing} />
                    <span className="ws-spacer" />
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
                                <div
                                    key={i}
                                    className="ws-msg"
                                    data-role={m.role}
                                    data-streaming={m.streaming ? 'true' : 'false'}
                                >
                                    {m.content}
                                </div>
                            ))}
                            <div ref={messagesEndRef} />
                        </div>

                        {/* PromptBar + Telemetry sibling cards (matches manifest §7.2) */}
                        <div className="ws-bottom">
                            <PromptBar
                                disabled={Boolean(hitlPending)}
                                placeholder={hitlPending ? `${nattName} is waiting for your decision` : undefined}
                                activeTaskId={activeTaskId}
                                isStreaming={isStreaming}
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
