import { useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import { useWorkspaceStore } from '../workspaceStore';
import type { AilienantConfig, ModelTier } from '../../shared/types';
import type { OrchestrationMode } from '../../shared/config';

export type ModelsView = 'switch' | 'orchestration' | 'usage' | 'preset' | 'thinking';

interface ModelInfo {
    id: string;
    name: string;
    provider: string;
    is_local: boolean;
}

interface TokenUsage {
    local_tokens: number;
    cloud_tokens: number;
    estimated_savings_usd: number;
    estimated_invested_usd: number;
}

interface ModelPreset {
    id: string;
    name: string;
    description: string;
    is_builtin: boolean;
    tiers: Record<string, string>;
}

interface BYOMConfigMsg {
    presets: ModelPreset[];
    active_preset_id: string | null;
}

// The Effort Budget — verification depth for the NEXT turn (mirrors
// GET/POST /api/v1/hardware/mode). Every level runs the same agents; the
// axis is how many verification layers a turn pays for, never which agents
// run, so no level is ever locked — cost_estimates states what it costs in
// local generation time instead.
type EffortLevel = 'light' | 'balanced' | 'deep';
interface EffortCostEstimate {
    extra_calls: string;
    seconds_per_extra_call: number;
    calibrated: boolean;
}
interface EffortModeMsg {
    mode: EffortLevel;
    cost_estimates: Record<EffortLevel, EffortCostEstimate>;
}

const EFFORT_LEVELS: EffortLevel[] = ['light', 'balanced', 'deep'];
const EFFORT_DESCRIPTIONS: Record<EffortLevel, string> = {
    light:    'Generate + syntax check only. No lint, no self-heal, no acceptance checks.',
    balanced: 'Adds a lint/type gate and up to 2 self-heal retries on failure.',
    deep:     'Adds running the plan\'s own acceptance checks before reporting done.',
};

interface Props {
    view: ModelsView;
    config: AilienantConfig | null;
    activeModelId: string;
    orchestrationMode: OrchestrationMode;
    onPrefChange: (activeModelId: string, orchestrationMode: OrchestrationMode) => void;
    onClose: () => void;
}

const TIERS: ModelTier[] = ['small', 'medium', 'big', 'cloud'];

export function ModelsMenu({ view, config, activeModelId, orchestrationMode, onPrefChange, onClose }: Props): JSX.Element {
    const [models, setModels] = useState<ModelInfo[] | null>(null);
    const [usage, setUsage] = useState<TokenUsage | null | 'loading'>('loading');
    const [byomConfig, setByomConfig] = useState<BYOMConfigMsg | null>(null);
    const [activating, setActivating] = useState<string | null>(null);
    const [effortMode, setEffortMode] = useState<EffortModeMsg | null>(null);
    const [effortSaving, setEffortSaving] = useState(false);
    // Phase 9 (ADR-707) — Native Thinking toggle is sourced from the persisted
    // workspace store (survives panel reload) and injected into SUBMIT_TASK.
    const nativeThinking = useWorkspaceStore(s => s.nativeThinking);
    const setNativeThinking = useWorkspaceStore(s => s.setNativeThinking);
    const autoAcceptLowRisk = useWorkspaceStore(s => s.autoAcceptLowRisk);
    const setAutoAcceptLowRisk = useWorkspaceStore(s => s.setAutoAcceptLowRisk);

    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as {
                type: string; models?: ModelInfo[]; usage?: TokenUsage | null;
                data?: BYOMConfigMsg | EffortModeMsg | null;
            };
            if (msg.type === 'MODELS_LIST') { setModels(msg.models ?? []); }
            else if (msg.type === 'USAGE_SNAPSHOT') { setUsage(msg.usage ?? null); }
            else if (msg.type === 'BYOM_CONFIG') {
                if (msg.data) { setByomConfig(msg.data as BYOMConfigMsg); setActivating(null); }
            } else if (msg.type === 'EFFORT_MODE') {
                if (msg.data) { setEffortMode(msg.data as EffortModeMsg); }
                setEffortSaving(false);
            }
        };
        window.addEventListener('message', handler);
        if (view === 'switch') { vscode.postMessage({ type: 'GET_MODELS' }); }
        if (view === 'usage') { vscode.postMessage({ type: 'GET_USAGE' }); }
        if (view === 'preset' || view === 'orchestration') { vscode.postMessage({ type: 'GET_BYOM_CONFIG' }); }
        if (view === 'orchestration') { vscode.postMessage({ type: 'GET_EFFORT_MODE' }); }
        return () => window.removeEventListener('message', handler);
    }, [view]);

    const handleActivatePreset = (presetId: string): void => {
        setActivating(presetId);
        vscode.postMessage({ type: 'ACTIVATE_PRESET', presetId });
    };

    const handleEffortChange = (mode: EffortLevel): void => {
        setEffortSaving(true);
        vscode.postMessage({ type: 'SET_EFFORT_MODE', mode });
    };

    const effortCostLabel = (level: EffortLevel): string => {
        const est = effortMode?.cost_estimates?.[level];
        if (!est) return '';
        if (est.extra_calls === '0') return 'No extra calls';
        const approx = est.calibrated ? '' : '~';
        return `+${est.extra_calls} call(s), ${approx}${est.seconds_per_extra_call}s each`;
    };

    // The active BYOM preset's real tier→model mapping — the same data the
    // 'preset' view above already fetches correctly via GET_BYOM_CONFIG.
    // `config.tiers` (the AilienantConfig prop) is sourced from a local
    // ailienant-config.json file that is never written anywhere in this
    // project, so it is always empty; kept only as a last-resort fallback.
    const activePresetTiers = byomConfig?.presets.find(p => p.id === byomConfig.active_preset_id)?.tiers;

    if (view === 'switch') {
        return (
            <div className="ws-models-body">
                {models === null ? (
                    <div className="ws-models-empty">Loading models…</div>
                ) : models.length === 0 ? (
                    <div className="ws-models-empty">
                        <span>No models discovered.</span>
                        <button
                            className="ws-core-menu-btn"
                            onClick={() => { vscode.postMessage({ type: 'OPEN_DASHBOARD', tab: 'byom' }); onClose(); }}
                        >
                            <Icon name="plug" size={13} /> Configure models →
                        </button>
                    </div>
                ) : (
                    <div className="ws-models-list">
                        {models.map(m => (
                            <button
                                key={m.id}
                                className="ws-models-row"
                                data-active={m.id === activeModelId ? 'true' : 'false'}
                                onClick={() => { onPrefChange(m.id, 'manual'); onClose(); }}
                            >
                                <div className="ws-models-row-text">
                                    <span className="ws-models-row-name">{m.name}</span>
                                    <span className="ws-models-row-meta">
                                        <span className="ws-tag">{m.provider}</span>
                                        <span className="ws-tag">{m.is_local ? 'local' : 'cloud'}</span>
                                    </span>
                                </div>
                                {m.id === activeModelId && <Icon name="check" size={13} />}
                            </button>
                        ))}
                    </div>
                )}
                <p className="ws-models-note">Selecting a model pins it as the preferred default (manual mode).</p>
            </div>
        );
    }

    if (view === 'orchestration') {
        return (
            <div className="ws-models-body">
                <button
                    className="ws-mode-row"
                    data-active={orchestrationMode === 'manual' ? 'true' : 'false'}
                    onClick={() => onPrefChange(activeModelId, 'manual')}
                >
                    <div className="ws-mode-row-text">
                        <span className="ws-mode-row-title">Manual — single model</span>
                        <span className="ws-mode-row-desc">
                            {activeModelId ? `Pinned: ${activeModelId}` : 'No model selected — pick one in Switch model'}
                        </span>
                    </div>
                </button>
                <button
                    className="ws-mode-row"
                    data-active={orchestrationMode === 'auto' ? 'true' : 'false'}
                    onClick={() => onPrefChange(activeModelId, 'auto')}
                >
                    <div className="ws-mode-row-text">
                        <span className="ws-mode-row-title">Auto — tiered orchestration</span>
                        <span className="ws-mode-row-desc">Router picks small / medium / big / cloud per task</span>
                    </div>
                </button>
                {orchestrationMode === 'auto' && (
                    <div className="ws-models-tiers">
                        {TIERS.map(t => (
                            <div key={t} className="ws-models-tier">
                                <span className="ws-models-tier-name">{t}</span>
                                <span className="ws-models-tier-model">{activePresetTiers?.[t] ?? config?.tiers?.[t] ?? '—'}</span>
                            </div>
                        ))}
                    </div>
                )}
                <p className="ws-models-note">Tier → model mapping is configured in the dashboard BYOM panel.</p>

                <div className="ws-models-section-title">Effort Budget</div>
                <p className="ws-models-note">
                    Verification depth for the next turn — not which agents run; every
                    level costs local generation time, never VRAM, so nothing here is locked.
                </p>
                {EFFORT_LEVELS.map(level => (
                    <button
                        key={level}
                        className="ws-mode-row"
                        data-active={effortMode?.mode === level ? 'true' : 'false'}
                        disabled={effortSaving}
                        onClick={() => handleEffortChange(level)}
                    >
                        <div className="ws-mode-row-text">
                            <span className="ws-mode-row-title" style={{ textTransform: 'capitalize' }}>{level}</span>
                            <span className="ws-mode-row-desc">
                                {EFFORT_DESCRIPTIONS[level]}
                                {effortMode ? ` — ${effortCostLabel(level)}` : ''}
                            </span>
                        </div>
                    </button>
                ))}
            </div>
        );
    }

    if (view === 'preset') {
        if (!byomConfig) {
            return <div className="ws-models-body"><div className="ws-models-empty">Loading presets…</div></div>;
        }
        const { presets, active_preset_id } = byomConfig;
        return (
            <div className="ws-models-body">
                {presets.length === 0 ? (
                    <div className="ws-models-empty">No presets available. Configure endpoints in the BYOM panel first.</div>
                ) : (
                    <div className="ws-models-list">
                        {presets.map(preset => {
                            const isActive = preset.id === active_preset_id;
                            const isBusy = activating === preset.id;
                            return (
                                <div key={preset.id} className="ws-models-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
                                    <div style={{ display: 'flex', alignItems: 'center', width: '100%', gap: 8 }}>
                                        <span className="ws-models-row-name">{preset.name}</span>
                                        {isActive && <Icon name="check" size={13} />}
                                        {!isActive && (
                                            <button
                                                className="ws-core-menu-btn"
                                                style={{ marginLeft: 'auto', fontSize: 11, padding: '2px 8px' }}
                                                disabled={isBusy}
                                                onClick={() => handleActivatePreset(preset.id)}>
                                                {isBusy ? 'Applying…' : 'Activate'}
                                            </button>
                                        )}
                                    </div>
                                    {preset.description && (
                                        <span className="ws-models-row-meta" style={{ fontSize: 11 }}>{preset.description}</span>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                )}
                <p className="ws-models-note">
                    Activating a preset rewrites the LiteLLM config and signals a proxy reload.
                    Manage presets in the <button className="ws-link-btn" onClick={() => { vscode.postMessage({ type: 'OPEN_DASHBOARD', tab: 'byom' }); onClose(); }}>BYOM panel</button>.
                </p>
            </div>
        );
    }

    if (view === 'thinking') {
        return (
            <div className="ws-models-body">
                <button
                    className="ws-mode-row"
                    role="switch"
                    aria-checked={nativeThinking}
                    data-active={nativeThinking ? 'true' : 'false'}
                    onClick={() => setNativeThinking(!nativeThinking)}
                >
                    <div className="ws-mode-row-text">
                        <span className="ws-mode-row-title">Native Thinking</span>
                        <span className="ws-mode-row-desc">
                            Stream the model's reasoning into a collapsible Thought Box
                            (Claude Extended Thinking / reasoning models). Falls back to
                            plain streaming on models that don't support it.
                        </span>
                    </div>
                    <span className="ws-toggle" data-on={nativeThinking ? 'true' : 'false'} aria-hidden="true">
                        {nativeThinking ? 'ON' : 'OFF'}
                    </span>
                </button>
                <p className="ws-models-note">
                    On by default for maximum reasoning. Turn off for lower-latency,
                    lower-cost replies — your choice is remembered across reloads.
                </p>

                <button
                    className="ws-mode-row"
                    role="switch"
                    aria-checked={autoAcceptLowRisk}
                    data-active={autoAcceptLowRisk ? 'true' : 'false'}
                    onClick={() => setAutoAcceptLowRisk(!autoAcceptLowRisk)}
                >
                    <div className="ws-mode-row-text">
                        <span className="ws-mode-row-title">Auto-accept low-risk edits</span>
                        <span className="ws-mode-row-desc">
                            Skip the approval card for edits the agent flags as low-risk and
                            apply them immediately. Medium- and high-risk actions always still
                            ask for your authorization.
                        </span>
                    </div>
                    <span className="ws-toggle" data-on={autoAcceptLowRisk ? 'true' : 'false'} aria-hidden="true">
                        {autoAcceptLowRisk ? 'ON' : 'OFF'}
                    </span>
                </button>
                <p className="ws-models-note">
                    Off by default. Use it for fast, repetitive flows where you trust the
                    agent's low-risk edits — you stay in control of anything riskier.
                </p>
            </div>
        );
    }

    // usage
    return (
        <div className="ws-models-body">
            {usage === 'loading' ? (
                <div className="ws-models-empty">Loading usage…</div>
            ) : usage === null ? (
                <div className="ws-models-empty">No usage recorded yet.</div>
            ) : (
                <div className="ws-usage-grid">
                    <div className="ws-usage-cell">
                        <span className="ws-usage-label">Local tokens</span>
                        <span className="ws-usage-value">{usage.local_tokens.toLocaleString()}</span>
                    </div>
                    <div className="ws-usage-cell">
                        <span className="ws-usage-label">Cloud tokens</span>
                        <span className="ws-usage-value">{usage.cloud_tokens.toLocaleString()}</span>
                    </div>
                    <div className="ws-usage-cell">
                        <span className="ws-usage-label">Est. cloud spend</span>
                        <span className="ws-usage-value">${usage.estimated_invested_usd.toFixed(2)}</span>
                    </div>
                    <div className="ws-usage-cell">
                        <span className="ws-usage-label">Est. local savings</span>
                        <span className="ws-usage-value">${usage.estimated_savings_usd.toFixed(2)}</span>
                    </div>
                </div>
            )}
        </div>
    );
}
