import { useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Icon, type IconName } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';
import type { WorkspaceSurface } from '../workspaceStore';
import type { DreamingProfile } from '../../shared/config';

interface Props {
    surface: WorkspaceSurface;
    onSurfaceChange: (s: WorkspaceSurface) => void;
    dreamingActive: boolean;
    dreamingProfile: DreamingProfile;
    onDreamingToggle: (active: boolean, profile: DreamingProfile) => void;
    /** Locked while a turn is streaming or a HITL decision is pending. */
    disabled?: boolean;
}

const SURFACES: { value: WorkspaceSurface; icon: IconName; label: string; desc: string }[] = [
    { value: 'chat',    icon: 'message',   label: 'Chat',    desc: 'Direct conversation — the agent acts on each request' },
    { value: 'planner', icon: 'clipboard', label: 'Planner', desc: 'Socratic ideation — the analyst grills you to co-author a plan' },
];

/**
 * Top-level interaction switcher (Chat ↔ Planner), plus a Dreaming entry that
 * reuses the existing memory-consolidation toggle/dashboard. Distinct from the
 * execution `ModeMenu` (which only governs permissions), this picks which
 * surface owns the composer.
 */
export function ModeSwitcher({
    surface, onSurfaceChange, dreamingActive, dreamingProfile, onDreamingToggle, disabled,
}: Props): JSX.Element {
    const [open, setOpen] = useState(false);
    const active = SURFACES.find(s => s.value === surface) ?? SURFACES[0];

    const pickSurface = (s: WorkspaceSurface): void => {
        onSurfaceChange(s);
        setOpen(false);
    };

    return (
        <Popover.Root open={open} onOpenChange={setOpen} modal={false}>
            <Tooltip content="Switch the active surface (Chat / Planner)">
                <Popover.Trigger asChild>
                    <button
                        className="ws-mode-trigger ai-btn"
                        data-variant="ghost"
                        disabled={disabled}
                        aria-label="Interaction surface"
                    >
                        <Icon name={active.icon} size={14} />
                        <span>{active.label}</span>
                        <Icon name="chevron-down" size={12} />
                    </button>
                </Popover.Trigger>
            </Tooltip>
            <Popover.Portal>
                <Popover.Content
                    className="ws-mode-menu"
                    side="top"
                    align="start"
                    sideOffset={6}
                    collisionPadding={8}
                >
                    <div className="ws-mode-section">
                        <div className="ws-mode-label">Surface</div>
                        {SURFACES.map(s => (
                            <label key={s.value} className="ws-mode-row" data-active={surface === s.value}>
                                <input
                                    type="radio"
                                    name="workspace-surface"
                                    checked={surface === s.value}
                                    onChange={() => pickSurface(s.value)}
                                />
                                <div className="ws-mode-row-text">
                                    <div className="ws-mode-row-title">
                                        <Icon name={s.icon} size={12} />
                                        <span>{s.label}</span>
                                    </div>
                                    <div className="ws-mode-row-desc">{s.desc}</div>
                                </div>
                            </label>
                        ))}
                    </div>

                    <hr className="ws-mode-hr" />

                    <div className="ws-mode-section">
                        <div className="ws-mode-label">Dreaming</div>
                        <label className="ws-mode-row" data-active={dreamingActive}>
                            <input
                                type="checkbox"
                                checked={dreamingActive}
                                onChange={() => onDreamingToggle(!dreamingActive, dreamingProfile)}
                            />
                            <div className="ws-mode-row-text">
                                <div className="ws-mode-row-title">
                                    <Icon name="moon" size={12} />
                                    <span>Background consolidation</span>
                                </div>
                                <div className="ws-mode-row-desc">Idle-time memory consolidation ({dreamingProfile})</div>
                            </div>
                        </label>
                        <button
                            className="ws-mode-row ws-mode-row-action"
                            onClick={() => { vscode.postMessage({ type: 'OPEN_DASHBOARD', tab: 'dreaming' }); setOpen(false); }}
                        >
                            <Icon name="external-link" size={12} />
                            <span>Open Dreaming dashboard</span>
                        </button>
                    </div>
                </Popover.Content>
            </Popover.Portal>
        </Popover.Root>
    );
}
