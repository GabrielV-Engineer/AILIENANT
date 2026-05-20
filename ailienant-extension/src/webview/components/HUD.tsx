import { useState, useCallback } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { ReasoningPreset, InferenceTier } from '../../shared/config';
import { PRESET_META } from '../hooks/useReasoningPreset';
import { TierToggle } from './TierToggle';

export interface ModelInfo {
    id: string;
    name: string;
    provider: string;
    is_local: boolean;
    context_window?: number;
    parameters_b?: number;
}

interface Props {
    preset: ReasoningPreset;
    tier: InferenceTier;
    disabled: boolean;
    models: ModelInfo[];
    selectedModelId: string | undefined;
    onPresetChange: (p: ReasoningPreset) => void;
    onTierChange: (t: InferenceTier) => void;
    onModelSelect: (id: string) => void;
}

const PRESETS: ReasoningPreset[] = ['surgeon', 'architect', 'explorer'];

export function HUD({
    preset, tier, disabled, models, selectedModelId,
    onPresetChange, onTierChange, onModelSelect,
}: Props): JSX.Element {
    const [expertOpen, setExpertOpen] = useState(false);

    const handlePreset = useCallback((p: ReasoningPreset) => {
        if (!disabled) { onPresetChange(p); }
    }, [disabled, onPresetChange]);

    return (
        <div className="ai-section ai-col" style={{ gap: 6 }}>
            {/* Level 1 — Reasoning Preset selector (Hick's Law: 3 choices) */}
            <div className="ai-row" style={{ gap: 4 }}>
                <div className="ai-preset-group" style={{ flex: 1 }}>
                    {PRESETS.map(p => {
                        const m = PRESET_META[p];
                        return (
                            <button
                                key={p}
                                className="ai-preset-btn"
                                data-active={preset === p ? 'true' : 'false'}
                                onClick={() => handlePreset(p)}
                                disabled={disabled}
                                title={m.desc}
                            >
                                <span className="ai-preset-icon">{m.icon}</span>
                                <span>{m.label}</span>
                            </button>
                        );
                    })}
                </div>

                {/* Level 2 — Expert popover trigger */}
                <Popover.Root open={expertOpen} onOpenChange={setExpertOpen}>
                    <Popover.Trigger asChild>
                        <button
                            className="ai-btn ai-btn-secondary"
                            style={{ padding: '4px 7px', fontSize: 13 }}
                            title="Expert model selector"
                            disabled={disabled}
                        >
                            ⚙️
                        </button>
                    </Popover.Trigger>
                    <Popover.Portal>
                        <Popover.Content
                            className="ai-popover-content"
                            side="top"
                            align="end"
                            sideOffset={4}
                        >
                            <div className="ai-popover-label">Model Override</div>
                            {models.length === 0 ? (
                                <div className="ai-muted">Loading models…</div>
                            ) : (
                                <div className="ai-model-list">
                                    {models.map(m => (
                                        <div
                                            key={m.id}
                                            className="ai-model-item"
                                            data-selected={selectedModelId === m.id ? 'true' : 'false'}
                                            onClick={() => { onModelSelect(m.id); setExpertOpen(false); }}
                                        >
                                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                {m.name}
                                            </span>
                                            <div style={{ display: 'flex', gap: 3, flexShrink: 0 }}>
                                                <span className="ai-model-badge">{m.provider}</span>
                                                {m.is_local && <span className="ai-model-badge">local</span>}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </Popover.Content>
                    </Popover.Portal>
                </Popover.Root>
            </div>

            {/* Inference Tier Toggle */}
            <TierToggle tier={tier} onChange={onTierChange} disabled={disabled} />
        </div>
    );
}
