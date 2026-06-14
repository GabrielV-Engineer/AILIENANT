import { useCallback, useRef, useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { CommandPalette, useSlashDetect } from './CommandPalette';
import { ContextOverlay } from './ContextOverlay';
import { DreamingMode } from './DreamingMode';
import { ModeMenu } from './ModeMenu';
import { MentionDropdown } from './MentionDropdown';
import { useAutoResizeTextarea } from '../hooks/useAutoResizeTextarea';
import { vscode } from '../vscode_bridge';
import type { AilienantConfig, ExecutionMode } from '../../shared/types';
import type {
    ReasoningPreset, DreamingProfile, OrchestrationMode, MentionItem,
} from '../../shared/config';
import { useWorkspaceStore } from '../workspaceStore';

interface Props {
    disabled: boolean;
    placeholder?: string;
    activeTaskId?: string;
    isStreaming: boolean;
    /** Phase 7.11.3 — true while a Stop click is in flight (optimistic UI). */
    isAborting: boolean;
    config: AilienantConfig | null;
    // Mode menu state
    mode: ExecutionMode;
    preset: ReasoningPreset;
    onModeChange:   (m: ExecutionMode) => void;
    onPresetChange: (p: ReasoningPreset) => void;
    // Dreaming
    dreamingActive: boolean;
    dreamingProfile: DreamingProfile;
    onDreamingToggle: (active: boolean, profile: DreamingProfile) => void;
    // Models menu preferences
    activeModelId: string;
    orchestrationMode: OrchestrationMode;
    onModelPrefChange: (activeModelId: string, orchestrationMode: OrchestrationMode) => void;
    /** Phase 7.12.9 (Fix 5) — scopes the prompt draft to this session. */
    sessionId: string;
    // Submit
    onSubmit: (text: string) => void;
    onAbort: () => void;
}

export function PromptBar({
    disabled, placeholder, activeTaskId, isStreaming, isAborting, config,
    mode, preset, onModeChange, onPresetChange,
    dreamingActive, dreamingProfile, onDreamingToggle,
    activeModelId, orchestrationMode, onModelPrefChange,
    sessionId, onSubmit, onAbort,
}: Props): JSX.Element {
    // Phase 7.11.2 — rehydrated panel-lifetime state via workspaceStore.
    // Phase 7.12.9 (Fix 5) — draft is keyed by sessionId so switching sessions
    // preserves each session's half-typed message instead of wiping it.
    const value          = useWorkspaceStore((s) => s.draftMessages[sessionId] ?? '');
    const setDraft       = useWorkspaceStore((s) => s.setDraft);
    const setValue       = useCallback((v: string) => setDraft(sessionId, v), [setDraft, sessionId]);
    const activeSkill    = useWorkspaceStore((s) => s.activeSkills[sessionId] ?? null);
    const setActiveSkill = useWorkspaceStore((s) => s.setActiveSkill);
    const contextOpen    = useWorkspaceStore((s) => s.contextOpen);
    const setContextOpen = useWorkspaceStore((s) => s.setContextOpen);
    const paletteOpen    = useWorkspaceStore((s) => s.paletteOpen);
    const setPaletteOpen = useWorkspaceStore((s) => s.setPaletteOpen);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    useAutoResizeTextarea(textareaRef, value);
    const { slashActive, slashQuery } = useSlashDetect(value);
    const paletteVisible = paletteOpen || slashActive;

    // ── Phase 7.11.4 — @mention autocomplete ─────────────────────────────
    // The caret-anchored regex makes the trigger fire only when the cursor
    // sits at the end of an @token, so clicking back into an existing @foo
    // mid-prompt does NOT re-pop the dropdown.
    const [caretPos, setCaretPos] = useState<number>(0);
    const [mentionResults, setMentionResults] = useState<MentionItem[]>([]);
    const [mentionActiveIdx, setMentionActiveIdx] = useState<number>(0);
    const { atActive, atQuery, atRange } = useAtMentionDetect(value, caretPos);
    // Palette wins when both could fire (plan W5).
    const mentionVisible = atActive && !paletteVisible;

    // Debounced query → host trie. UI debounce = 80 ms (the trie itself is
    // debounced 500 ms on the FS side; this 80 ms keeps the autocomplete
    // snappy for typing while still coalescing keystrokes).
    useEffect(() => {
        if (!mentionVisible) { return; }
        const h = setTimeout(() => {
            vscode.postMessage({ type: 'WORKSPACE_PATHS_QUERY', prefix: atQuery });
        }, 80);
        return () => clearTimeout(h);
    }, [mentionVisible, atQuery]);

    // Reset highlight when the result set changes.
    useEffect(() => { setMentionActiveIdx(0); }, [mentionResults]);

    // Insert an @-mention path returned by the host's workspace file quick-pick.
    // The store's getState() always returns the current draft, so the listener
    // does NOT need to re-bind on every keystroke (mount-once / unmount-once).
    useEffect(() => {
        const handler = (e: MessageEvent): void => {
            const msg = e.data as {
                type: string;
                path?: string;
                text?: string;
                id?: string;
                name?: string;
                results?: MentionItem[];
            };
            if (msg.type === 'INSERT_MENTION' && msg.path) {
                const v = useWorkspaceStore.getState().draftMessages[sessionId] ?? '';
                setValue(`${v}${v && !v.endsWith(' ') ? ' ' : ''}@${msg.path} `);
                setPaletteOpen(false);
                textareaRef.current?.focus();
            }
            // Skill chip: attach an explicit skill to the next submit without
            // polluting the user's draft text. Replaces the old INSERT_PROMPT raw-paste.
            if (msg.type === 'INVOKE_SKILL' && msg.id && msg.name) {
                setActiveSkill(sessionId, { id: msg.id, name: msg.name });
                textareaRef.current?.focus();
            }
            // Autocomplete results from the host-side path index.
            if (msg.type === 'WORKSPACE_PATHS_RESULT' && Array.isArray(msg.results)) {
                // Always append a constant @terminal entry so it's reachable
                // from any query (the stub opens the existing ContextOverlay
                // terminal tab via OPEN_CONTEXT_TERMINAL).
                const TERMINAL_ROW: MentionItem = { kind: 'terminal', path: '@terminal' };
                setMentionResults([...msg.results, TERMINAL_ROW]);
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
    }, [setValue, setPaletteOpen, setActiveSkill, sessionId]);

    /** Phase 7.11.4 — apply a selected mention into the textarea: splice it
     *  over the active `@…` range so the partial query is replaced atomically. */
    const insertMention = useCallback((item: MentionItem) => {
        if (item.kind === 'terminal') {
            // Stub: drop the @terminal token, open the ContextOverlay terminal
            // tab. Honest about the lack of a public VS Code terminal-output API.
            const before = value.slice(0, atRange.start);
            const after  = value.slice(atRange.end);
            setValue(`${before}${after}`);
            vscode.postMessage({ type: 'OPEN_CONTEXT_TERMINAL' });
            textareaRef.current?.focus();
            return;
        }
        const tokenPrefix = item.kind === 'folder' ? '@folder:' : '@file:';
        const before = value.slice(0, atRange.start);
        const after  = value.slice(atRange.end);
        const replacement = `${tokenPrefix}${item.path} `;
        const next = `${before}${replacement}${after}`;
        setValue(next);
        // Park the caret right after the inserted token.
        const newCaret = atRange.start + replacement.length;
        requestAnimationFrame(() => {
            const ta = textareaRef.current;
            if (ta) {
                ta.focus();
                ta.setSelectionRange(newCaret, newCaret);
            }
        });
    }, [value, atRange.start, atRange.end, setValue]);

    const submit = useCallback(() => {
        const text = value.trim();
        if (!text || disabled) { return; }
        onSubmit(text);
        setValue('');
        setPaletteOpen(false);
        setActiveSkill(sessionId, null);
    }, [value, disabled, onSubmit, setValue, setPaletteOpen, sessionId, setActiveSkill]);

    const onKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        // Phase 7.11.4 — mention-dropdown keyboard navigation. Palette wins
        // when both could fire (the palette has its own handlers).
        if (mentionVisible && !paletteVisible && mentionResults.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setMentionActiveIdx(i => Math.min(i + 1, mentionResults.length - 1));
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setMentionActiveIdx(i => Math.max(i - 1, 0));
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                const pick = mentionResults[mentionActiveIdx];
                if (pick) { insertMention(pick); }
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                // Closing the dropdown without selecting — drop the @-token so
                // a stale @prefix doesn't keep re-opening the menu.
                setMentionResults([]);
                return;
            }
        }
        // Esc stops an in-flight task — the same affordance as the Stop button, so
        // the keyboard mirrors the single source of truth (no separate stop key).
        if (e.key === 'Escape' && isStreaming && !isAborting && !paletteVisible && !mentionVisible) {
            e.preventDefault();
            onAbort();
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey && !paletteVisible && !mentionVisible) {
            e.preventDefault();
            submit();
        }
    }, [submit, paletteVisible, mentionVisible, mentionResults, mentionActiveIdx, insertMention, isStreaming, isAborting, onAbort]);

    /** Track the textarea caret so `useAtMentionDetect` can anchor on it. */
    const updateCaret = useCallback(() => {
        const ta = textareaRef.current;
        if (ta) { setCaretPos(ta.selectionStart ?? 0); }
    }, []);

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
            {/* Phase 7.11.4 — @mention autocomplete (palette has priority). */}
            {mentionVisible && (
                <MentionDropdown
                    query={atQuery}
                    results={mentionResults}
                    activeIdx={mentionActiveIdx}
                    onSelect={insertMention}
                    onHoverIdx={setMentionActiveIdx}
                />
            )}
            {/* Active skill chip — cleared on submit or manual removal. */}
            {activeSkill && (
                <div className="ws-prompt-skill-chip" style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '2px 6px', marginBottom: 4, background: 'var(--vscode-badge-background)', borderRadius: 3, fontSize: 11, maxWidth: '100%', overflow: 'hidden' }}>
                    <Icon name="wand" size={11} />
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{activeSkill.name}</span>
                    <button
                        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, lineHeight: 1, opacity: 0.7, flexShrink: 0 }}
                        onClick={() => setActiveSkill(sessionId, null)}
                        aria-label="Remove skill"
                    >
                        <Icon name="x" size={10} />
                    </button>
                </div>
            )}
            {/* Top row: textarea */}
            <div className="ws-prompt-input-row">
                <textarea
                    ref={textareaRef}
                    className="ws-prompt-input"
                    rows={1}
                    value={value}
                    onChange={(e) => { setValue(e.target.value); updateCaret(); }}
                    onKeyDown={onKey}
                    onKeyUp={updateCaret}
                    onClick={updateCaret}
                    onSelect={updateCaret}
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
                        disabled={disabled}
                    />
                </div>

                <div className="ws-prompt-tools-right">
                    <ModeMenu
                        mode={mode}
                        preset={preset}
                        disabled={disabled}
                        onModeChange={onModeChange}
                        onPresetChange={onPresetChange}
                    />
                    {isStreaming ? (
                        <Tooltip content={isAborting ? 'Aborting…' : 'Stop current task (Esc)'} side="top">
                            <button
                                className="ai-btn ws-send-btn"
                                data-variant="danger"
                                data-state={isAborting ? 'aborting' : undefined}
                                onClick={onAbort}
                                disabled={isAborting}
                                aria-label="Stop task"
                            >
                                <Icon name="square" size={11} />
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

/**
 * Phase 7.11.4 — caret-anchored @mention trigger detection.
 *
 * Mirrors `useSlashDetect`'s shape but matches an `@token` anywhere in the
 * prompt, not just at start-of-line. The regex is run against
 * `value.slice(0, caretPos)` so the dropdown only opens when the caret sits
 * at the END of an active @token — clicking back into an existing `@foo`
 * mid-prompt does NOT re-pop the menu (plan W2).
 *
 * Returns:
 *   - `atActive` — true while the caret sits at the end of an @-token.
 *   - `atQuery`  — the path fragment after the `@…:` prefix (or after the
 *     bare `@`, which acts as a "show everything" query).
 *   - `atRange`  — `{start, end}` over the source `value`; the caller uses
 *     this to splice in the selected mention atomically.
 */
export function useAtMentionDetect(
    value: string,
    caretPos: number,
): { atActive: boolean; atQuery: string; atRange: { start: number; end: number } } {
    const head = value.slice(0, caretPos);
    // @file:foo  /  @folder:bar  /  @terminal  /  bare @  with optional path chars.
    const m = /@(file:|folder:|terminal\b)?([\w./\-]*)$/.exec(head);
    if (!m) {
        return { atActive: false, atQuery: '', atRange: { start: caretPos, end: caretPos } };
    }
    // Require that the @ is at start-of-string or preceded by whitespace —
    // avoids matching emails ("user@host") and inline mentions inside text.
    const atIdx = head.length - m[0].length;
    if (atIdx > 0) {
        const prev = head[atIdx - 1];
        if (prev !== ' ' && prev !== '\t' && prev !== '\n') {
            return { atActive: false, atQuery: '', atRange: { start: caretPos, end: caretPos } };
        }
    }
    const query = m[2] ?? '';
    return {
        atActive: true,
        atQuery: query,
        atRange: { start: atIdx, end: caretPos },
    };
}
