import { useState, useCallback } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { ReasoningPreset, InferenceTier } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
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
        <div className="ws-hud">
            <div className="ws-preset-row">
                <div className="ws-preset-group">
                    {PRESETS.map(p => {
                        const m = PRESET_META[p];
                        return (
                            <Tooltip key={p} content={m.desc}>
                                <button
                                    className="ws-preset-btn"
                                    data-active={preset === p ? 'true' : 'false'}
                                    onClick={() => handlePreset(p)}
                                    disabled={disabled}
                                    aria-label={`${m.label} reasoning preset`}
                                >
                                    <Icon name={m.icon} size={15} />
                                    <span>{m.label}</span>
                                </button>
                            </Tooltip>
                        );
                    })}
                </div>

                <Popover.Root open={expertOpen} onOpenChange={setExpertOpen}>
                    <Tooltip content="Expert model picker — override the active LLM">
                        <Popover.Trigger asChild>
                            <button
                                className="ai-btn"
                                data-variant="ghost"
                                style={{ padding: '6px 8px' }}
                                disabled={disabled}
                                aria-label="Expert model selector"
                            >
                                <Icon name="settings" size={15} />
                            </button>
                        </Popover.Trigger>
                    </Tooltip>
                    <Popover.Portal>
                        <Popover.Content
                            className="ws-popover"
                            side="bottom"
                            align="end"
                            sideOffset={6}
                        >
                            <div className="ws-popover-label">Model Override</div>
                            {models.length === 0 ? (
                                <div className="ws-muted">No models loaded.</div>
                            ) : (
                                <div className="ws-model-list">
                                    {models.map(m => (
                                        <button
                                            key={m.id}
                                            className="ws-model-item"
                                            data-selected={selectedModelId === m.id ? 'true' : 'false'}
                                            onClick={() => { onModelSelect(m.id); setExpertOpen(false); }}
                                        >
                                            <span className="ws-model-name">{m.name}</span>
                                            <span className="ws-model-tags">
                                                <span className="ws-tag">{m.provider}</span>
                                                {m.is_local && <span className="ws-tag" data-kind="local">local</span>}
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </Popover.Content>
                    </Popover.Portal>
                </Popover.Root>
            </div>

            <TierToggle tier={tier} onChange={onTierChange} disabled={disabled} />
        </div>
    );
}
