import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

interface Props {
    disabled?: boolean;
}

// Static themes the user can scope a consolidation pass to. A focused pass
// restructures memory toward one concern and spends fewer tokens than "Auto".
const FOCUS_PRESETS: string[] = [
    'Architecture and Patterns',
    'Refactoring and Technical Debt',
    'Bug Fixes',
];

export function DreamingTrigger({ disabled }: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const [otherMode, setOtherMode] = useState(false);
    const [customText, setCustomText] = useState('');

    const run = (focusArea: string | null): void => {
        vscode.postMessage({ type: 'TRIGGER_DREAMING_RUN', focus_area: focusArea });
        setOpen(false);
        setOtherMode(false);
        setCustomText('');
    };

    const submitCustom = (): void => {
        const trimmed = customText.trim();
        if (trimmed) { run(trimmed); }
    };

    return (
        <Popover.Root
            open={open}
            onOpenChange={(next) => { setOpen(next); if (!next) { setOtherMode(false); } }}
        >
            <Tooltip content="Consolidate memory (manual Dreaming)">
                <Popover.Trigger asChild>
                    <button
                        className="ws-prompt-icon-btn ws-dream-manual-btn"
                        aria-label="Consolidate memory"
                        disabled={disabled}
                    >
                        <Icon name="sparkles" size={15} />
                    </button>
                </Popover.Trigger>
            </Tooltip>
            <Popover.Portal>
                <Popover.Content
                    className="ws-dream-menu ws-dream-manual-menu"
                    side="top"
                    align="start"
                    sideOffset={6}
                    collisionPadding={8}
                >
                    <div className="ws-dream-head">
                        <div>
                            <div className="ws-dream-title">Consolidate Memory</div>
                            <div className="ws-muted">Pick a focus, or let it run wide</div>
                        </div>
                    </div>

                    <hr className="ws-mode-hr" />

                    <div className="ws-dream-list">
                        {FOCUS_PRESETS.map((focus) => (
                            <button
                                key={focus}
                                className="ws-dream-row ws-dream-manual-row"
                                onClick={() => run(focus)}
                            >
                                <span className="ws-dream-row-label">{focus}</span>
                            </button>
                        ))}

                        <button
                            className="ws-dream-row ws-dream-manual-row"
                            data-variant="auto"
                            onClick={() => run(null)}
                        >
                            <span className="ws-dream-row-label">Auto (whole workspace)</span>
                        </button>

                        {otherMode ? (
                            <input
                                type="text"
                                className="ws-dream-manual-input"
                                autoFocus
                                placeholder="Describe a focus, then press Enter…"
                                value={customText}
                                onChange={(e) => setCustomText(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter') { e.preventDefault(); submitCustom(); }
                                    if (e.key === 'Escape') { setOtherMode(false); }
                                }}
                            />
                        ) : (
                            <button
                                className="ws-dream-row ws-dream-manual-row"
                                data-variant="other"
                                onClick={() => setOtherMode(true)}
                            >
                                <span className="ws-dream-row-label">Other…</span>
                            </button>
                        )}
                    </div>

                    <div className="ws-muted ws-tiny ws-dream-footer">
                        Runs once, on demand. A save mid-run aborts it.
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
