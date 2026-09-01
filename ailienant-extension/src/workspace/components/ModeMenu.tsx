import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import {
    ExecutionMode, EXECUTION_MODE_LABELS, EXECUTION_MODE_DESCRIPTIONS,
} from '../../shared/types';

interface Props {
    mode: ExecutionMode;
    disabled?: boolean;
    onModeChange:   (m: ExecutionMode) => void;
}

const MODES: ExecutionMode[] = ['automatic', 'ask_before_edits', 'plan_mode'];

export function ModeMenu({
    mode, disabled,
    onModeChange,
}: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const trigger = `${EXECUTION_MODE_LABELS[mode]}`;

    return (
        <Popover.Root open={open} onOpenChange={setOpen} modal={false}>
            <Tooltip content="Pick execution mode">
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
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
