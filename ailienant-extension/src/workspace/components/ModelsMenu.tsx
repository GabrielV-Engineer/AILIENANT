import { useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import type { AilienantConfig, ModelTier } from '../../shared/types';
import type { OrchestrationMode } from '../../shared/config';

export type ModelsView = 'switch' | 'orchestration' | 'usage' | 'preset';

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

    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; models?: ModelInfo[]; usage?: TokenUsage | null; data?: BYOMConfigMsg };
            if (msg.type === 'MODELS_LIST') { setModels(msg.models ?? []); }
            else if (msg.type === 'USAGE_SNAPSHOT') { setUsage(msg.usage ?? null); }
            else if (msg.type === 'BYOM_CONFIG') {
                if (msg.data) { setByomConfig(msg.data); setActivating(null); }
            }
        };
        window.addEventListener('message', handler);
        if (view === 'switch') { vscode.postMessage({ type: 'GET_MODELS' }); }
        if (view === 'usage') { vscode.postMessage({ type: 'GET_USAGE' }); }
        if (view === 'preset') { vscode.postMessage({ type: 'GET_BYOM_CONFIG' }); }
        return () => window.removeEventListener('message', handler);
    }, [view]);

    const handleActivatePreset = (presetId: string): void => {
        setActivating(presetId);
        vscode.postMessage({ type: 'ACTIVATE_PRESET', presetId });
    };

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
                                <span className="ws-models-tier-model">{config?.tiers?.[t] ?? '—'}</span>
                            </div>
                        ))}
                    </div>
                )}
                <p className="ws-models-note">Tier → model mapping is configured in the dashboard BYOM panel.</p>
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
                    Manage presets in the <button className="ws-core-menu-btn" style={{ display: 'inline', padding: 0, background: 'none', border: 'none', color: 'var(--accent-primary, #63a583)', cursor: 'pointer', fontSize: 'inherit' }} onClick={() => { vscode.postMessage({ type: 'OPEN_DASHBOARD', tab: 'byom' }); onClose(); }}>BYOM panel</button>.
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
