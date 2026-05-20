import './index.css';
import { createRoot } from 'react-dom/client';
import { useState, useCallback, useEffect, useRef, useReducer } from 'react';
import { vscode } from './vscode_bridge';
import {
    IntelligenceProfile, ReasoningPreset, InferenceTier,
    DreamingProfile, WsConnectionStatus, OccStatus,
    TelemetryFrame, TokenSnapshot,
} from '../shared/config';
import { DEFAULT_PROFILE } from '../shared/config';
import { MasterToggle } from './components/MasterToggle';
import { HUD, ModelInfo } from './components/HUD';
import { TelemetryHUD, useTpsCalculator } from './components/TelemetryHUD';
import { DreamingMode } from './components/DreamingMode';
import { CSSAlertBanner } from './components/CSSAlertBanner';
import { SlashMenu, useSlashDetect } from './components/SlashMenu';
import { HITLCard, HITLRequest } from './components/HITLCard';
import { BentoMenu } from './BentoMenu';
import { getPresetConfig } from './hooks/useReasoningPreset';

// ── Toast ────────────────────────────────────────────────────────────────────
type ToastLevel = 'info' | 'warn' | 'error';
interface Toast { id: number; level: ToastLevel; message: string }
let _toastId = 0;

function ToastStack({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }): JSX.Element {
    return (
        <div className="ai-toast-stack">
            {toasts.map(t => (
                <div
                    key={t.id}
                    className="ai-toast"
                    data-level={t.level}
                    onClick={() => onDismiss(t.id)}
                    role="alert"
                >
                    {t.message}
                </div>
            ))}
        </div>
    );
}

// ── Chat message ─────────────────────────────────────────────────────────────
interface Message { role: 'user' | 'assistant'; content: string; streaming?: boolean }

// ── Initial state ─────────────────────────────────────────────────────────────
interface InitialState {
    masterEnabled:   boolean;
    profile:         IntelligenceProfile;
    reasoningPreset: ReasoningPreset;
    inferenceTier:   InferenceTier;
    dreamingEnabled: boolean;
    dreamingProfile: DreamingProfile;
}

function readInitialState(root: HTMLElement): InitialState {
    const raw = root.dataset.initial;
    const defaults: InitialState = {
        masterEnabled:   false,
        profile:         DEFAULT_PROFILE,
        reasoningPreset: 'architect',
        inferenceTier:   'HYBRID',
        dreamingEnabled: false,
        dreamingProfile: 'Hybrid',
    };
    if (!raw) { return defaults; }
    try {
        return { ...defaults, ...(JSON.parse(raw) as Partial<InitialState>) };
    } catch {
        return defaults;
    }
}

// ── App ───────────────────────────────────────────────────────────────────────
function App({ initial, logoUri }: { initial: InitialState; logoUri: string }): JSX.Element {
    // Core state
    const [enabled,         setEnabled]         = useState(initial.masterEnabled);
    const [preset,          setPreset]           = useState<ReasoningPreset>(initial.reasoningPreset);
    const [tier,            setTier]             = useState<InferenceTier>(initial.inferenceTier);
    const [dreamingActive,  setDreamingActive]   = useState(initial.dreamingEnabled);
    const [dreamingProfile, setDreamingProfile]  = useState<DreamingProfile>(initial.dreamingProfile);

    // Chat
    const [messages,   setMessages]   = useState<Message[]>([]);
    const [inputValue, setInputValue] = useState('');
    const [isStreaming, setIsStreaming] = useState(false);
    const [activeTaskId, setActiveTaskId] = useState<string | undefined>();
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Telemetry
    const [wsStatus,    setWsStatus]    = useState<WsConnectionStatus>('disconnected');
    const [occStatus,   setOccStatus]   = useState<OccStatus>('clear');
    const [lockedFiles, setLockedFiles] = useState(0);
    const [telemetry,   setTelemetry]   = useState<TelemetryFrame | undefined>();
    const [snapshot,    setSnapshot]    = useState<TokenSnapshot | undefined>();
    const [budgetUsd,   setBudgetUsd]   = useState(10.0);
    const [fileBlocked, setFileBlocked] = useState(false);
    const [blockedFile, setBlockedFile] = useState<string | undefined>();

    // Models
    const [models,          setModels]          = useState<ModelInfo[]>([]);
    const [selectedModelId, setSelectedModelId] = useState<string | undefined>();

    // HITL
    const [hitlQueue, setHitlQueue] = useState<HITLRequest[]>([]);

    // DLQ badge
    const [dlqCount, setDlqCount] = useState(0);

    // Panel visibility
    const [showBento, setShowBento] = useState(false);

    // Toasts
    const [toasts, setToasts] = useState<Toast[]>([]);
    const addToast = useCallback((level: ToastLevel, message: string) => {
        const id = ++_toastId;
        setToasts(prev => [...prev.slice(-2), { id, level, message }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 6000);
    }, []);
    const dismissToast = useCallback((id: number) => {
        setToasts(prev => prev.filter(t => t.id !== id));
    }, []);

    // TPS calculator
    const { recordChunk, tps, history: tpsHistory } = useTpsCalculator();

    // Slash menu
    const { slashActive, slashQuery } = useSlashDetect(inputValue);

    // ── WS message handler ──────────────────────────────────────────────────
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
                    setMessages(prev =>
                        prev.map((m, i) => i === prev.length - 1 ? { ...m, streaming: false } : m)
                    );
                    break;
                case 'server_telemetry': {
                    const frame = msg.payload as TelemetryFrame;
                    setTelemetry(frame);
                    if (frame.is_red_alert && !telemetry?.is_red_alert) {
                        addToast('error', `⚠️ CSS Alert: context ${frame.css_total.toFixed(0)}% — inject more files`);
                    }
                    break;
                }
                case 'server_hitl_approval_request':
                    setHitlQueue(prev => [...prev, msg.payload as HITLRequest]);
                    addToast('warn', '🔑 Human approval required');
                    break;
                case 'server_model_warmup': {
                    const d = msg.payload as { model_name: string; is_local: boolean };
                    addToast('info', `🔥 Warming up ${d.model_name} (${d.is_local ? 'local' : 'cloud'})`);
                    break;
                }
                case 'OOM_ENGAGED':
                    addToast('error', '🚨 OOM detected — falling back to cloud model');
                    break;
                case 'FILE_BLOCKED': {
                    const d = msg.payload as { blocked: boolean; filePath?: string };
                    setFileBlocked(d.blocked);
                    setBlockedFile(d.filePath);
                    if (d.blocked) {
                        setOccStatus('hard_conflict');
                    } else if (occStatus === 'hard_conflict') {
                        setOccStatus('clear');
                    }
                    break;
                }
                case 'OCC_CONFLICT': {
                    setOccStatus('soft_conflict');
                    setLockedFiles(prev => prev + 1);
                    addToast('warn', '⚠️ Concurrency conflict detected — review before submitting');
                    break;
                }
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
                case 'DLQ_COUNT':
                    setDlqCount(msg.payload as number);
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
    }, [addToast, occStatus, recordChunk, telemetry]);

    // Auto-scroll chat
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    // ── Submit task ──────────────────────────────────────────────────────────
    const handleSubmit = useCallback(() => {
        const text = inputValue.trim();
        if (!text || !enabled || fileBlocked || isStreaming) { return; }

        const presetConfig = getPresetConfig(preset);
        setMessages(prev => [...prev, { role: 'user', content: text }]);
        setInputValue('');

        vscode.postMessage({
            type:  'SUBMIT_TASK',
            value: text,
            preset,
            tier,
            ...presetConfig,
            model_override: selectedModelId,
        });
    }, [inputValue, enabled, fileBlocked, isStreaming, preset, tier, selectedModelId]);

    const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey && !slashActive) {
            e.preventDefault();
            handleSubmit();
        }
    }, [handleSubmit, slashActive]);

    // ── Dreaming toggle ──────────────────────────────────────────────────────
    const handleDreamingToggle = useCallback((next: boolean, profile: DreamingProfile) => {
        setDreamingActive(next);
        setDreamingProfile(profile);
    }, []);

    // ── Root data-* attributes for CSS mode accents ──────────────────────────
    const rootAttrs: Record<string, string> = {
        'data-dreaming': dreamingActive ? 'true' : 'false',
        'data-tier':     tier,
        'data-alert':    telemetry?.is_red_alert ? 'true' : 'false',
    };

    return (
        <div
            style={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}
            {...rootAttrs}
        >
            {/* Logo header */}
            {logoUri && (
                <div className="ai-sidebar-header">
                    <img src={logoUri} alt="AILIENANT" className="ai-logo" />
                </div>
            )}

            {/* Connection health bar */}
            <div className="ai-connection-bar">
                <span className="ai-connection-dot" data-status={wsStatus} />
                <span>{wsStatus === 'connected' ? 'Connected' : wsStatus === 'reconnecting' ? 'Reconnecting…' : 'Disconnected'}</span>
                <span className="ai-spacer" />
                {dlqCount > 0 && (
                    <span className="ai-dlq-badge" title={`${dlqCount} unresolved DLQ episodes`}>
                        {dlqCount}
                    </span>
                )}
            </div>

            {/* Master toggle + Dreaming button */}
            <div className="ai-section ai-row" style={{ justifyContent: 'space-between' }}>
                <div style={{ flex: 1 }}>
                    <MasterToggle active={enabled} onChange={setEnabled} />
                </div>
                <DreamingMode
                    active={dreamingActive}
                    profile={dreamingProfile}
                    onToggle={handleDreamingToggle}
                />
            </div>

            {/* HUD: Reasoning preset + Tier toggle + Expert model */}
            <HUD
                preset={preset}
                tier={tier}
                disabled={!enabled}
                models={models}
                selectedModelId={selectedModelId}
                onPresetChange={setPreset}
                onTierChange={setTier}
                onModelSelect={setSelectedModelId}
            />

            {/* Telemetry instruments */}
            <TelemetryHUD
                occStatus={occStatus}
                lockedFiles={lockedFiles}
                tps={tps}
                tpsHistory={tpsHistory}
                snapshot={snapshot}
                budgetUsd={budgetUsd}
                telemetry={telemetry}
            />

            {/* CSS alert banner */}
            <CSSAlertBanner telemetry={telemetry} />

            {/* File blocked warning */}
            {fileBlocked && (
                <div className="ai-alert-banner" style={{ margin: '0 8px 4px' }}>
                    <span>🔒</span>
                    <span>
                        <strong>{blockedFile ? blockedFile.split(/[\\/]/).pop() : 'Active file'}</strong> is
                        blocked by <code>.ailienantignore</code>. Cloud forwarding disabled.
                    </span>
                </div>
            )}

            {/* HITL approval queue */}
            {hitlQueue.map(req => (
                <HITLCard
                    key={req.approval_id}
                    request={req}
                    onResolved={id => setHitlQueue(prev => prev.filter(r => r.approval_id !== id))}
                />
            ))}

            {/* Chat messages */}
            <div className="ai-messages" style={{ flex: 1 }}>
                {messages.length === 0 && (
                    <div className="ai-muted" style={{ textAlign: 'center', marginTop: 24 }}>
                        AILIENANT ready. Type a task or use{' '}
                        <strong style={{ color: 'var(--ai-accent)' }}>/</strong> for commands.
                    </div>
                )}
                {messages.map((m, i) => (
                    <div
                        key={i}
                        className={`ai-bubble${m.streaming ? ' ai-bubble-stream' : ''}`}
                        data-role={m.role}
                    >
                        {m.content}
                    </div>
                ))}
                <div ref={messagesEndRef} />
            </div>

            {/* Bento menu (collapsible) */}
            {showBento && <BentoMenu disabled={!enabled} />}

            {/* Input area */}
            <div className="ai-section" style={{ position: 'relative' }}>
                {slashActive && (
                    <SlashMenu
                        query={slashQuery}
                        onClose={() => setInputValue('')}
                        activeTaskId={activeTaskId}
                        onCommandSelect={cmd => setInputValue(cmd + ' ')}
                    />
                )}
                <div className="ai-row" style={{ gap: 6, alignItems: 'flex-end' }}>
                    {/* Quick actions */}
                    <button
                        className="ai-btn ai-btn-secondary"
                        style={{ padding: '4px 6px', fontSize: 13, flexShrink: 0 }}
                        onClick={() => setShowBento(b => !b)}
                        title="Agent Launcher (Bento)"
                        disabled={!enabled}
                    >
                        ⊞
                    </button>
                    <button
                        className="ai-btn ai-btn-secondary"
                        style={{ padding: '4px 6px', fontSize: 13, flexShrink: 0 }}
                        onClick={() => setInputValue('/')}
                        title="Slash commands"
                        disabled={!enabled}
                    >
                        /
                    </button>

                    {/* Text input */}
                    <textarea
                        className="ai-chat-input"
                        rows={2}
                        value={inputValue}
                        onChange={e => setInputValue(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder={
                            fileBlocked
                                ? '🔒 File blocked — cannot send'
                                : enabled
                                    ? 'Message AILIENANT… (Enter to send, Shift+Enter newline)'
                                    : 'Enable AILIENANT to start'
                        }
                        disabled={!enabled || fileBlocked}
                        aria-label="Task input"
                    />

                    {/* Submit / Abort */}
                    {isStreaming ? (
                        <button
                            className="ai-btn ai-btn-danger"
                            style={{ flexShrink: 0, padding: '4px 8px' }}
                            onClick={() => vscode.postMessage({ type: 'ABORT_TASK' })}
                        >
                            ■
                        </button>
                    ) : (
                        <button
                            className="ai-btn ai-btn-primary"
                            style={{ flexShrink: 0, padding: '4px 10px' }}
                            onClick={handleSubmit}
                            disabled={!enabled || fileBlocked || !inputValue.trim()}
                        >
                            ➤
                        </button>
                    )}
                </div>
            </div>

            {/* Toast stack */}
            <ToastStack toasts={toasts} onDismiss={dismissToast} />
        </div>
    );
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    const initial = readInitialState(root);
    const logoUri = root.dataset.logo ?? '';
    createRoot(root).render(<App initial={initial} logoUri={logoUri} />);
});
