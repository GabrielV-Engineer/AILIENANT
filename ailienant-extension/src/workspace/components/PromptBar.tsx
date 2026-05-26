import { useCallback, useRef, useEffect } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { CommandPalette, useSlashDetect } from './CommandPalette';
import { ContextOverlay } from './ContextOverlay';
import { DreamingMode } from './DreamingMode';
import { ModeMenu } from './ModeMenu';
import type { AilienantConfig, ExecutionMode } from '../../shared/types';
import type { ReasoningPreset, InferenceTier, DreamingProfile, OrchestrationMode } from '../../shared/config';
import { useWorkspaceStore } from '../workspaceStore';

interface Props {
    disabled: boolean;
    placeholder?: string;
    activeTaskId?: string;
    isStreaming: boolean;
    config: AilienantConfig | null;
    // Mode menu state
    mode: ExecutionMode;
    preset: ReasoningPreset;
    tier: InferenceTier;
    onModeChange:   (m: ExecutionMode) => void;
    onPresetChange: (p: ReasoningPreset) => void;
    onTierChange:   (t: InferenceTier) => void;
    // Dreaming
    dreamingActive: boolean;
    dreamingProfile: DreamingProfile;
    onDreamingToggle: (active: boolean, profile: DreamingProfile) => void;
    // Models menu preferences
    activeModelId: string;
    orchestrationMode: OrchestrationMode;
    onModelPrefChange: (activeModelId: string, orchestrationMode: OrchestrationMode) => void;
    // Submit
    onSubmit: (text: string) => void;
    onAbort: () => void;
}

export function PromptBar({
    disabled, placeholder, activeTaskId, isStreaming, config,
    mode, preset, tier, onModeChange, onPresetChange, onTierChange,
    dreamingActive, dreamingProfile, onDreamingToggle,
    activeModelId, orchestrationMode, onModelPrefChange,
    onSubmit, onAbort,
}: Props): JSX.Element {
    // Phase 7.11.2 — rehydrated panel-lifetime state via workspaceStore.
    const value          = useWorkspaceStore((s) => s.inputDraft);
    const setValue       = useWorkspaceStore((s) => s.setInputDraft);
    const contextOpen    = useWorkspaceStore((s) => s.contextOpen);
    const setContextOpen = useWorkspaceStore((s) => s.setContextOpen);
    const paletteOpen    = useWorkspaceStore((s) => s.paletteOpen);
    const setPaletteOpen = useWorkspaceStore((s) => s.setPaletteOpen);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { slashActive, slashQuery } = useSlashDetect(value);
    const paletteVisible = paletteOpen || slashActive;

    // Insert an @-mention path returned by the host's workspace file quick-pick.
    // The store's getState() always returns the current draft, so the listener
    // does NOT need to re-bind on every keystroke (mount-once / unmount-once).
    useEffect(() => {
        const handler = (e: MessageEvent): void => {
            const msg = e.data as { type: string; path?: string; text?: string };
            if (msg.type === 'INSERT_MENTION' && msg.path) {
                const v = useWorkspaceStore.getState().inputDraft;
                setValue(`${v}${v && !v.endsWith(' ') ? ' ' : ''}@${msg.path} `);
                setPaletteOpen(false);
                textareaRef.current?.focus();
            }
            // Phase 7.9.A.7.f — insert a saved skill template into the prompt.
            if (msg.type === 'INSERT_PROMPT' && msg.text) {
                const v = useWorkspaceStore.getState().inputDraft;
                setValue(v.trim() ? `${v.replace(/\s*$/, '')}\n${msg.text}` : msg.text!);
                setPaletteOpen(false);
                textareaRef.current?.focus();
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
    }, [setValue, setPaletteOpen]);

    const submit = useCallback(() => {
        const text = value.trim();
        if (!text || disabled) { return; }
        onSubmit(text);
        setValue('');
        setPaletteOpen(false);
    }, [value, disabled, onSubmit]);

    const onKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey && !paletteVisible) {
            e.preventDefault();
            submit();
        }
    }, [submit, paletteVisible]);

    return (
        <div className="ws-prompt">
            {contextOpen && (
                <ContextOverlay onClose={() => setContextOpen(false)} />
            )}
            {paletteVisible && (
                <CommandPalette
                    query={slashActive ? slashQuery : ''}
                    activeTaskId={activeTaskId}
                    config={config}
                    activeModelId={activeModelId}
                    orchestrationMode={orchestrationMode}
                    onPrefChange={onModelPrefChange}
                    onOpenContext={() => { setContextOpen(true); setPaletteOpen(false); if (slashActive) { setValue(''); } }}
                    onClose={() => { setPaletteOpen(false); if (slashActive) { setValue(''); } }}
                />
            )}
            {/* Top row: textarea */}
            <div className="ws-prompt-input-row">
                <textarea
                    ref={textareaRef}
                    className="ws-prompt-input"
                    rows={1}
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={onKey}
                    placeholder={placeholder ?? 'Submit your request…'}
                    disabled={disabled}
                    aria-label="Prompt input"
                />
            </div>

            {/* Bottom row: tools left + mode/send right */}
            <div className="ws-prompt-tools">
                <div className="ws-prompt-tools-left">
                    <Tooltip content="Attach files, terminal output, or directories to the prompt context">
                        <button
                            className="ws-prompt-icon-btn"
                            onClick={() => { setContextOpen(!contextOpen); setPaletteOpen(false); }}
                            aria-label="Add context"
                            disabled={disabled}
                        >
                            <Icon name="plus" size={15} />
                        </button>
                    </Tooltip>
                    <Tooltip content="Open the command menu (type / in the input for quick access)">
                        <button
                            className="ws-prompt-icon-btn"
                            onClick={() => { setPaletteOpen(!paletteOpen); setContextOpen(false); }}
                            aria-label="Commands"
                            disabled={disabled}
                        >
                            <Icon name="wand" size={15} />
                        </button>
                    </Tooltip>
                    <DreamingMode
                        active={dreamingActive}
                        profile={dreamingProfile}
                        config={config}
                        onToggle={onDreamingToggle}
                    />
                </div>

                <div className="ws-prompt-tools-right">
                    <ModeMenu
                        mode={mode}
                        preset={preset}
                        tier={tier}
                        disabled={disabled}
                        onModeChange={onModeChange}
                        onPresetChange={onPresetChange}
                        onTierChange={onTierChange}
                    />
                    {isStreaming ? (
                        <Tooltip content="Abort current task" side="top">
                            <button
                                className="ai-btn ws-send-btn"
                                data-variant="danger"
                                onClick={onAbort}
                                aria-label="Abort task"
                            >
                                <Icon name="x" size={12} />
                            </button>
                        </Tooltip>
                    ) : (
                        <Tooltip content="Send prompt (Enter)" side="top">
                            <button
                                className="ai-btn ws-send-btn"
                                data-variant="primary"
                                onClick={submit}
                                disabled={disabled || !value.trim()}
                                aria-label="Send"
                            >
                                <Icon name="send" size={12} />
                            </button>
                        </Tooltip>
                    )}
                </div>
            </div>
        </div>
    );
}
