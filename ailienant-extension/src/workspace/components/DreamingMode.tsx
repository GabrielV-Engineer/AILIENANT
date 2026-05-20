import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { DreamingProfile } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

interface Props {
    active: boolean;
    profile: DreamingProfile;
    onToggle: (next: boolean, profile: DreamingProfile) => void;
}

const PROFILES: { value: DreamingProfile; label: string; desc: string }[] = [
    { value: 'Medium', label: 'Medium', desc: 'Llama 3.1 8B · 1 task · 3 files · <60min' },
    { value: 'Big',    label: 'Big',    desc: 'Qwen 32B / 70B · 3 tasks · 10 files · nightly' },
    { value: 'Cloud',  label: 'Cloud',  desc: 'Claude / GPT · 1 task · 5 files · token-capped' },
    { value: 'Hybrid', label: 'Hybrid', desc: 'Cloud System 2 + Local System 1.5' },
];

export function DreamingMode({ active, profile, onToggle }: Props): JSX.Element {
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
                        className="ws-dream-btn"
                        data-active={active ? 'true' : 'false'}
                        aria-label="Dreaming mode"
                    >
                        <Icon name="moon" size={14} />
                        <span>{active ? 'Dreaming' : 'Dream'}</span>
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
                    <div className="ws-popover-header">
                        <div>
                            <div className="ws-popover-label">Dreaming Mode</div>
                            <div className="ws-muted">Autonomous background optimization</div>
                        </div>
                        <Tooltip content="Toggle dreaming on/off">
                            <label className="ws-switch" aria-label="Toggle dreaming">
                                <input type="checkbox" checked={active} onChange={handleToggle} />
                                <span className="ws-switch-track" />
                                <span className="ws-switch-thumb" />
                            </label>
                        </Tooltip>
                    </div>

                    <hr className="ws-hr" />

                    <div className="ws-popover-label">Profile</div>
                    <div className="ws-profile-list">
                        {PROFILES.map(p => (
                            <label key={p.value} className="ws-profile-item" data-active={currentProfile === p.value}>
                                <input
                                    type="radio"
                                    name="dreaming-profile"
                                    value={p.value}
                                    checked={currentProfile === p.value}
                                    onChange={() => handleProfileChange(p.value)}
                                />
                                <div className="ws-profile-text">
                                    <div className="ws-profile-name">{p.label}</div>
                                    <div className="ws-muted">{p.desc}</div>
                                </div>
                            </label>
                        ))}
                    </div>

                    <hr className="ws-hr" />

                    <div className="ws-muted ws-tiny">
                        Blast radius: 8 files / session.<br />
                        L1 Local → L2 Cloud-Fixer → L3 Circuit Breaker.
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
