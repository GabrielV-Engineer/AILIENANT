import { useState, useCallback, useEffect, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { vscode } from './vscode_bridge';
import {
    ReasoningPreset, InferenceTier, DreamingProfile,
    WsConnectionStatus, OccStatus, TelemetryFrame, TokenSnapshot,
} from '../shared/config';
import type { AilienantConfig } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { Icon } from '../shared/Icon';
import { WorkspaceHeader } from './components/WorkspaceHeader';
import { HUD, type ModelInfo } from './components/HUD';
import { TelemetryHUD, useTpsCalculator } from './components/TelemetryHUD';
import { DreamingMode } from './components/DreamingMode';
import { CSSAlertBanner } from './components/CSSAlertBanner';
import { PromptBar } from './components/PromptBar';
import { NattCanvas } from './components/NattCanvas';
import { ConfigPanel } from './components/ConfigPanel';
import type { HITLIntervention } from './components/HITLInterventionCard';
import { getPresetConfig } from './hooks/useReasoningPreset';

type ToastLevel = 'info' | 'warn' | 'error';
interface ToastItem { id: number; level: ToastLevel; message: string; }
let _toastId = 0;

interface Message { role: 'user' | 'assistant'; content: string; streaming?: boolean; }

interface InitialState {
    sessionId: string;
    sessionTitle: string;
    config: AilienantConfig | null;
    logoUri: string;
}

export function Workspace({ initial }: { initial: InitialState }): JSX.Element {
    const config = initial.config;
    const nattName = config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;

    // Core state
    const [preset, setPreset] = useState<ReasoningPreset>('architect');
    const [tier, setTier] = useState<InferenceTier>('HYBRID');
    const [dreamingActive, setDreamingActive] = useState(false);
    const [dreamingProfile, setDreamingProfile] = useState<DreamingProfile>('Hybrid');
    const [budgetUsd, setBudgetUsd] = useState(config?.finops?.budget_usd ?? 10.0);

    // Chat
    const [messages, setMessages] = useState<Message[]>([]);
    const [isStreaming, setIsStreaming] = useState(false);
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Natt
    const [nattOpen, setNattOpen] = useState(false);
    const [nattMessages] = useState<{ role: 'natt' | 'user'; content: string }[]>([]);
    const [hitlPending, setHitlPending] = useState<HITLIntervention | undefined>();

    // Telemetry
    const [wsStatus, setWsStatus] = useState<WsConnectionStatus>('disconnected');
    const [occStatus, setOccStatus] = useState<OccStatus>('clear');
    const [lockedFiles, setLockedFiles] = useState(0);
    const [telemetry, setTelemetry] = useState<TelemetryFrame | undefined>();
    const [snapshot, setSnapshot] = useState<TokenSnapshot | undefined>();

    // Models
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [selectedModelId, setSelectedModelId] = useState<string | undefined>();

    // Toasts
    const [toasts, setToasts] = useState<ToastItem[]>([]);
    const addToast = useCallback((level: ToastLevel, message: string) => {
        const id = ++_toastId;
        setToasts(prev => [...prev.slice(-2), { id, level, message }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 6000);
    }, []);

    const { recordChunk, tps, history: tpsHistory } = useTpsCalculator();

    // ── WS / extension message handler ─────────────────────────
    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; payload?: unknown };

            switch (msg.type) {
                case 'WS_STATUS':
                    setWsStatus(msg.payload as WsConnectionStatus);
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
                case 'MODELS_LOADED':
                    setModels(msg.payload as ModelInfo[]);
                    break;
                case 'TASK_STARTED': {
                    const d = msg.payload as { task_id: string };
                    setActiveTaskId(d.task_id);
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
            ...presetConfig,
            model_override: selectedModelId,
            session_id: initial.sessionId,
        });
    }, [preset, tier, selectedModelId, initial.sessionId]);

    const handleAbort = useCallback(() => {
        vscode.postMessage({ type: 'ABORT_TASK' });
    }, []);

    const handleDreamingToggle = useCallback((next: boolean, p: DreamingProfile) => {
        setDreamingActive(next);
        setDreamingProfile(p);
    }, []);

    const handleEngineChange = useCallback((tierKey: 'small' | 'medium' | 'big' | 'cloud') => {
        const m = config?.tiers[tierKey];
        if (m) { setSelectedModelId(m); }
    }, [config]);

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
        wsStatus === 'connected' ? 'Connected' :
        wsStatus === 'reconnecting' ? 'Reconnecting…' : 'Disconnected';

    return (
        <Tooltip.Provider delayDuration={400} skipDelayDuration={150}>
            <div className="ws-root" {...rootAttrs}>
                <WorkspaceHeader
                    sessionTitle={initial.sessionTitle}
                    nattName={nattName}
                    nattOpen={nattOpen}
                    onToggleNatt={() => setNattOpen(o => !o)}
                    onOpenSettings={() => { /* settings dropdown is inline */ }}
                    logoUri={initial.logoUri}
                />

                {/* Status strip */}
                <div className="ws-status">
                    <span className="ws-status-dot" data-status={wsStatus} />
                    <span>{wsLabel}</span>
                    <span style={{ flex: 1 }} />
                    <DreamingMode
                        active={dreamingActive}
                        profile={dreamingProfile}
                        onToggle={handleDreamingToggle}
                    />
                </div>

                {/* Main split-grid */}
                <main className="ws-main">
                    {/* LEFT: chat + controls */}
                    <section className="ws-main-left">
                        <CSSAlertBanner telemetry={telemetry} />

                        {config && (
                            <ConfigPanel
                                config={config}
                                budgetUsd={budgetUsd}
                                onBudgetChange={setBudgetUsd}
                                onEngineChange={handleEngineChange}
                                onOpenContextOverlay={() => { /* triggered from PromptBar [+] */ }}
                            />
                        )}

                        <HUD
                            preset={preset}
                            tier={tier}
                            disabled={false}
                            models={models}
                            selectedModelId={selectedModelId}
                            onPresetChange={setPreset}
                            onTierChange={setTier}
                            onModelSelect={setSelectedModelId}
                        />

                        <TelemetryHUD
                            occStatus={occStatus}
                            lockedFiles={lockedFiles}
                            tps={tps}
                            tpsHistory={tpsHistory}
                            snapshot={snapshot}
                            budgetUsd={budgetUsd}
                            telemetry={telemetry}
                        />

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

                        <PromptBar
                            disabled={Boolean(hitlPending)}
                            placeholder={hitlPending ? `${nattName} is waiting for your decision` : undefined}
                            activeTaskId={activeTaskId}
                            isStreaming={isStreaming}
                            onSubmit={handleSubmit}
                            onAbort={handleAbort}
                        />
                    </section>

                    {/* RIGHT: Natt pane (visible only when nattOpen) */}
                    {nattOpen && (
                        <NattCanvas
                            nattName={nattName}
                            messages={nattMessages}
                            pendingIntervention={hitlPending}
                            onClose={() => setNattOpen(false)}
                            onResolveIntervention={handleResolveHitl}
                        />
                    )}
                </main>

                {/* Toast stack */}
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
