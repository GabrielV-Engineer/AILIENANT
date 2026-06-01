import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { ReasoningPreset } from '../../shared/config';
import {
    ExecutionMode, EXECUTION_MODE_LABELS, EXECUTION_MODE_DESCRIPTIONS,
} from '../../shared/types';
import { PRESET_META } from '../hooks/useReasoningPreset';

interface Props {
    mode: ExecutionMode;
    preset: ReasoningPreset;
    disabled?: boolean;
    onModeChange:   (m: ExecutionMode) => void;
    onPresetChange: (p: ReasoningPreset) => void;
}

const MODES: ExecutionMode[] = ['automatic', 'ask_before_edits', 'plan_mode'];
const PRESETS: ReasoningPreset[] = ['surgeon', 'architect', 'explorer'];

export function ModeMenu({
    mode, preset, disabled,
    onModeChange, onPresetChange,
}: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const trigger = `${EXECUTION_MODE_LABELS[mode]}`;

    return (
        <Popover.Root open={open} onOpenChange={setOpen} modal={false}>
            <Tooltip content="Pick execution mode and reasoning preset">
                <Popover.Trigger asChild>
                    <button
                        className="ws-mode-trigger ai-btn"
                        data-variant="ghost"
                        disabled={disabled}
                        aria-label="Execution mode"
                    >
                        <Icon name="settings" size={14} />
                        <span>{trigger}</span>
                        <Icon name="chevron-down" size={12} />
                    </button>
                </Popover.Trigger>
            </Tooltip>
            <Popover.Portal>
                <Popover.Content
                    className="ws-mode-menu"
                    side="top"
                    align="end"
                    sideOffset={6}
                    collisionPadding={8}
                >
                    {/* Section 1: Execution mode */}
                    <div className="ws-mode-section">
                        <div className="ws-mode-label">Execution mode</div>
                        {MODES.map(m => (
                            <label key={m} className="ws-mode-row" data-active={mode === m}>
                                <input
                                    type="radio"
                                    name="execution-mode"
                                    checked={mode === m}
                                    onChange={() => onModeChange(m)}
                                />
                                <div className="ws-mode-row-text">
                                    <div className="ws-mode-row-title">{EXECUTION_MODE_LABELS[m]}</div>
                                    <div className="ws-mode-row-desc">{EXECUTION_MODE_DESCRIPTIONS[m]}</div>
                                </div>
                            </label>
                        ))}
                    </div>

                    <hr className="ws-mode-hr" />

                    {/* Section 2: Reasoning preset */}
                    <div className="ws-mode-section">
                        <div className="ws-mode-label">Reasoning preset</div>
                        {PRESETS.map(p => {
                            const meta = PRESET_META[p];
                            return (
                                <label key={p} className="ws-mode-row" data-active={preset === p}>
                                    <input
                                        type="radio"
                                        name="reasoning-preset"
                                        checked={preset === p}
                                        onChange={() => onPresetChange(p)}
                                    />
                                    <div className="ws-mode-row-text">
                                        <div className="ws-mode-row-title">
                                            <Icon name={meta.icon} size={12} />
                                            <span>{meta.label}</span>
                                        </div>
                                        <div className="ws-mode-row-desc">{meta.desc}</div>
                                    </div>
                                </label>
                            );
                        })}
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
