import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { DreamingProfile } from '../../shared/config';
import type { AilienantConfig } from '../../shared/types';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

interface Props {
    active: boolean;
    profile: DreamingProfile;
    config: AilienantConfig | null;
    onToggle: (next: boolean, profile: DreamingProfile) => void;
}

interface ProfileMeta {
    value: DreamingProfile;
    label: string;
    limits: string;
}

const PROFILES: ProfileMeta[] = [
    { value: 'Medium', label: 'Medium', limits: '1 task · 3 files · 60min' },
    { value: 'Big',    label: 'Big',    limits: '3 tasks · 10 files · nightly' },
    { value: 'Cloud',  label: 'Cloud',  limits: '1 task · 5 files · token-capped' },
    { value: 'Hybrid', label: 'Hybrid', limits: 'Cloud thinks, local edits · 2 tasks · 6 files' },
];

function modelForProfile(p: DreamingProfile, config: AilienantConfig | null): string {
    if (!config) { return '—'; }
    switch (p) {
        case 'Medium': return config.tiers.medium ?? '—';
        case 'Big':    return config.tiers.big ?? '—';
        case 'Cloud':  return config.tiers.cloud ?? '—';
        case 'Hybrid': return '—';
    }
}

export function DreamingMode({ active, profile, config, onToggle }: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const [currentProfile, setCurrentProfile] = useState<DreamingProfile>(profile);

    const handleToggle = (): void => {
        const next = !active;
        onToggle(next, currentProfile);
        vscode.postMessage({ type: 'dreaming_toggle', value: next, profile: currentProfile });
    };

    const handleProfileChange = (p: DreamingProfile): void => {
        setCurrentProfile(p);
        if (active) {
            onToggle(true, p);
            vscode.postMessage({ type: 'dreaming_toggle', value: true, profile: p });
        }
    };

    const triggerLabel = active ? `Dreaming (${currentProfile}) — click to configure` : 'Start Dreaming mode';

    return (
        <Popover.Root open={open} onOpenChange={setOpen}>
            <Tooltip content={triggerLabel}>
                <Popover.Trigger asChild>
                    <button
                        className="ws-prompt-icon-btn ws-dream-btn"
                        data-active={active ? 'true' : 'false'}
                        aria-label="Dreaming mode"
                    >
                        <Icon name="moon" size={15} />
                    </button>
                </Popover.Trigger>
            </Tooltip>
            <Popover.Portal>
                <Popover.Content
                    className="ws-dream-menu"
                    side="top"
                    align="start"
                    sideOffset={6}
                    collisionPadding={8}
                >
                    <div className="ws-dream-head">
                        <div>
                            <div className="ws-dream-title">Dreaming Mode</div>
                            <div className="ws-muted">Autonomous background optimization</div>
                        </div>
                        <Tooltip content="Toggle dreaming on / off">
                            <label className="ws-switch" aria-label="Toggle dreaming">
                                <input type="checkbox" checked={active} onChange={handleToggle} />
                                <span className="ws-switch-track" />
                                <span className="ws-switch-thumb" />
                            </label>
                        </Tooltip>
                    </div>

                    <hr className="ws-mode-hr" />

                    <div className="ws-mode-label">Profile</div>
                    <div className="ws-dream-list">
                        {PROFILES.map(p => (
                            <label
                                key={p.value}
                                className="ws-dream-row"
                                data-active={currentProfile === p.value}
                            >
                                <input
                                    type="radio"
                                    name="dreaming-profile"
                                    value={p.value}
                                    checked={currentProfile === p.value}
                                    onChange={() => handleProfileChange(p.value)}
                                />
                                <div className="ws-dream-row-text">
                                    <div className="ws-dream-row-top">
                                        <span className="ws-dream-row-label">{p.label}</span>
                                        <span className="ws-dream-row-model">{modelForProfile(p.value, config)}</span>
                                    </div>
                                    <div className="ws-dream-row-limits">{p.limits}</div>
                                </div>
                            </label>
                        ))}
                    </div>

                    <div className="ws-muted ws-tiny ws-dream-footer">
                        Runs in the background. Stops if errors compound.
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
