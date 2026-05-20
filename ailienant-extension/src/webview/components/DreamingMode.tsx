import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { DreamingProfile } from '../../shared/config';
import { vscode } from '../vscode_bridge';

interface Props {
    active: boolean;
    profile: DreamingProfile;
    onToggle: (next: boolean, profile: DreamingProfile) => void;
}

const PROFILES: { value: DreamingProfile; label: string; desc: string }[] = [
    { value: 'Medium',  label: 'Medium',  desc: 'Llama 3.1 8B · 1 task · 3 files · <60min' },
    { value: 'Big',     label: 'Big',     desc: 'Qwen 32B / 70B · 3 tasks · 10 files · Nightly' },
    { value: 'Cloud',   label: 'Cloud',   desc: 'Claude/GPT · 1 task · 5 files · Token capped' },
    { value: 'Hybrid',  label: 'Hybrid ⚡', desc: 'Cloud System 2 + Local System 1.5' },
];

export function DreamingMode({ active, profile, onToggle }: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const [currentProfile, setCurrentProfile] = useState<DreamingProfile>(profile);

    const handleToggleSwitch = (): void => {
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

    return (
        <Popover.Root open={open} onOpenChange={setOpen}>
            <Popover.Trigger asChild>
                <button
                    className="ai-dream-btn"
                    data-active={active ? 'true' : 'false'}
                    title={active ? `Dreaming (${currentProfile}) — click to configure` : 'Start Dreaming mode'}
                    aria-label="Dreaming mode"
                >
                    🌙
                    <span style={{ fontSize: 11 }}>{active ? 'Dreaming' : 'Dream'}</span>
                </button>
            </Popover.Trigger>
            <Popover.Portal>
                <Popover.Content
                    className="ai-popover-content"
                    side="top"
                    align="end"
                    sideOffset={4}
                >
                    <div className="ai-popover-row">
                        <div>
                            <div className="ai-popover-label">🌙 Dreaming Mode</div>
                            <div className="ai-muted">Autonomous background optimization</div>
                        </div>
                        <label className="ai-switch" aria-label="Toggle dreaming mode">
                            <input
                                type="checkbox"
                                checked={active}
                                onChange={handleToggleSwitch}
                            />
                            <span className="ai-switch-track" />
                            <span className="ai-switch-thumb" />
                        </label>
                    </div>

                    <hr className="ai-hr" />

                    <div className="ai-popover-label" style={{ marginBottom: 4 }}>Profile</div>
                    <div className="ai-profile-pills">
                        {PROFILES.map(p => (
                            <label key={p.value} className="ai-profile-pill">
                                <input
                                    type="radio"
                                    name="dreaming-profile"
                                    value={p.value}
                                    checked={currentProfile === p.value}
                                    onChange={() => handleProfileChange(p.value)}
                                />
                                <div>
                                    <div style={{ fontWeight: 500 }}>{p.label}</div>
                                    <div className="ai-muted">{p.desc}</div>
                                </div>
                            </label>
                        ))}
                    </div>

                    <hr className="ai-hr" />

                    <div className="ai-muted" style={{ fontSize: 10 }}>
                        Blast radius: max 8 files/session<br />
                        L1 Local → L2 Cloud-Fixer → L3 Circuit Breaker
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
