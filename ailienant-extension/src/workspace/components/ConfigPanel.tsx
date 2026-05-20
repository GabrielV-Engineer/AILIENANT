import { useState } from 'react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import * as Slider from '@radix-ui/react-slider';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import type { AilienantConfig } from '../../shared/types';

interface Props {
    config: AilienantConfig;
    budgetUsd: number;
    onBudgetChange: (next: number) => void;
    onEngineChange: (tierKey: 'small' | 'medium' | 'big' | 'cloud') => void;
    onOpenContextOverlay: () => void;
}

export function ConfigPanel({
    config, budgetUsd, onBudgetChange, onEngineChange, onOpenContextOverlay,
}: Props): JSX.Element {
    const [activeTier, setActiveTier] = useState<'small' | 'medium' | 'big' | 'cloud'>('medium');

    const handleTierSelect = (k: 'small' | 'medium' | 'big' | 'cloud'): void => {
        setActiveTier(k);
        onEngineChange(k);
    };

    const tiers: Array<{ key: 'small' | 'medium' | 'big' | 'cloud'; model: string | undefined }> = [
        { key: 'small',  model: config.tiers.small },
        { key: 'medium', model: config.tiers.medium },
        { key: 'big',    model: config.tiers.big },
        { key: 'cloud',  model: config.tiers.cloud },
    ];

    return (
        <div className="ws-config ai-card">
            {/* Engine dropdown */}
            <div className="ws-config-control">
                <div className="ws-config-label">Active engine</div>
                <DropdownMenu.Root>
                    <Tooltip content="Choose which tier mapping to use as the active engine">
                        <DropdownMenu.Trigger asChild>
                            <button className="ai-btn ws-engine-trigger">
                                <Icon name="cpu" size={14} />
                                <span>{activeTier} · {tiers.find(t => t.key === activeTier)?.model ?? '—'}</span>
                                <Icon name="chevron-down" size={14} />
                            </button>
                        </DropdownMenu.Trigger>
                    </Tooltip>
                    <DropdownMenu.Portal>
                        <DropdownMenu.Content className="ws-dropdown" align="start" sideOffset={4}>
                            {tiers.map(t => (
                                <DropdownMenu.Item
                                    key={t.key}
                                    className="ws-dropdown-item"
                                    data-active={activeTier === t.key}
                                    onSelect={() => handleTierSelect(t.key)}
                                    disabled={!t.model}
                                >
                                    <span className="ws-dropdown-tier">{t.key}</span>
                                    <span className="ws-dropdown-model">{t.model ?? 'not configured'}</span>
                                </DropdownMenu.Item>
                            ))}
                        </DropdownMenu.Content>
                    </DropdownMenu.Portal>
                </DropdownMenu.Root>
            </div>

            {/* FinOps slider */}
            <div className="ws-config-control">
                <div className="ws-config-label">
                    <span>Session budget cap</span>
                    <span className="ws-config-value">${budgetUsd.toFixed(2)}</span>
                </div>
                <Tooltip content="Maximum dollar ceiling for this agent session">
                    <Slider.Root
                        className="ws-slider"
                        value={[budgetUsd]}
                        onValueChange={(v) => onBudgetChange(v[0])}
                        min={1}
                        max={100}
                        step={1}
                    >
                        <Slider.Track className="ws-slider-track">
                            <Slider.Range className="ws-slider-range" />
                        </Slider.Track>
                        <Slider.Thumb className="ws-slider-thumb" aria-label="Budget cap" />
                    </Slider.Root>
                </Tooltip>
            </div>

            {/* Context adder — exposed here as a quick action; also lives inline in PromptBar */}
            <div className="ws-config-control ws-config-control--inline">
                <div className="ws-config-label">Context</div>
                <Tooltip content="Open the context attachment overlay">
                    <button
                        className="ai-btn"
                        data-variant="ghost"
                        onClick={onOpenContextOverlay}
                        aria-label="Add context"
                    >
                        <Icon name="plus" size={14} /><span>Add</span>
                    </button>
                </Tooltip>
            </div>
        </div>
    );
}
