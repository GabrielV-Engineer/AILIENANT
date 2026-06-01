import { useCallback, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

interface Props {
    /** Locked while a HITL decision is pending (mirrors PromptBar). */
    disabled: boolean;
    isStreaming: boolean;
    isAborting: boolean;
    /**
     * True once the analyst has streamed at least one question this session.
     * Until then the turn is the opening brief — agreement is meaningless, so
     * the synthesize action stays disabled (mirrors `analyst._has_prior_socratic_exchange`).
     */
    canAgree: boolean;
    nattName: string;
    onSubmit: (text: string) => void;
    onAbort: () => void;
    onExit: () => void;
}

/**
 * The literal phrase that flips the backend Socratic loop into synthesis.
 * `analyst._is_agreement` does a case-insensitive substring match against its
 * `_AGREEMENT_SIGNALS` frozenset; this phrase matches both "looks good" and
 * "proceed". Keep it in sync with that set — it is the sole frontend↔backend
 * coupling point for the Planner handoff.
 */
const AGREEMENT_SIGNAL = 'Looks good, proceed.';

/**
 * Blocked multi-turn Socratic surface. Reuses the shared transcript above it
 * (the analyst's questions arrive as ordinary streamed assistant messages); this
 * component owns only the planner-scoped composer and the agree/exit affordances.
 * Every submit is tagged `planner_mode_active` upstream so the backend keeps the
 * turn inside the ideation loop.
 */
export function PlannerSession({
    disabled, isStreaming, isAborting, canAgree, nattName, onSubmit, onAbort, onExit,
}: Props): JSX.Element {
    const [value, setValue] = useState('');
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const locked = disabled || isStreaming;

    const submit = useCallback(() => {
        const text = value.trim();
        if (!text || locked) { return; }
        onSubmit(text);
        setValue('');
    }, [value, locked, onSubmit]);

    const agree = useCallback(() => {
        if (!canAgree || locked) { return; }
        onSubmit(AGREEMENT_SIGNAL);
        setValue('');
        // Synthesis hands off to autonomous execution — optimistically return to
        // Chat so the next turn streams normally.
        onExit();
    }, [canAgree, locked, onSubmit, onExit]);

    const onKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submit();
        }
    }, [submit]);

    const placeholder = canAgree
        ? `Answer ${nattName}, or synthesize the plan when you're aligned…`
        : `Describe what you want to build — ${nattName} will grill you into a plan…`;

    return (
        <div className="ws-prompt ws-planner">
            <div className="ws-planner-banner">
                <div className="ws-planner-banner-text">
                    <Icon name="clipboard" size={14} color="var(--accent-primary)" />
                    <span><strong>Planner</strong> — Socratic ideation with {nattName}</span>
                </div>
                <Tooltip content="Leave Planner and return to Chat" side="top">
                    <button
                        className="ws-prompt-icon-btn"
                        onClick={onExit}
                        aria-label="Exit planner"
                    >
                        <Icon name="x" size={14} />
                    </button>
                </Tooltip>
            </div>

            <div className="ws-prompt-input-row">
                <textarea
                    ref={textareaRef}
                    className="ws-prompt-input"
                    rows={1}
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={onKey}
                    placeholder={placeholder}
                    disabled={locked}
                    aria-label="Planner input"
                />
            </div>

            <div className="ws-prompt-tools">
                <div className="ws-prompt-tools-left">
                    <Tooltip content={canAgree
                        ? 'Signal agreement — the analyst synthesizes the plan and hands off to execution'
                        : 'Answer at least one question before synthesizing'} side="top">
                        <span>
                            <button
                                className="ai-btn"
                                data-variant="ghost"
                                onClick={agree}
                                disabled={!canAgree || locked}
                                aria-label="Agree and synthesize plan"
                            >
                                <Icon name="check" size={13} />
                                <span>Agree &amp; synthesize</span>
                            </button>
                        </span>
                    </Tooltip>
                </div>

                <div className="ws-prompt-tools-right">
                    {isStreaming ? (
                        <Tooltip content={isAborting ? 'Aborting…' : 'Abort current turn'} side="top">
                            <button
                                className="ai-btn ws-send-btn"
                                data-variant="danger"
                                data-state={isAborting ? 'aborting' : undefined}
                                onClick={onAbort}
                                disabled={isAborting}
                                aria-label="Abort turn"
                            >
                                <Icon name="x" size={12} />
                            </button>
                        </Tooltip>
                    ) : (
                        <Tooltip content="Send answer (Enter)" side="top">
                            <button
                                className="ai-btn ws-send-btn"
                                data-variant="primary"
                                onClick={submit}
                                disabled={locked || !value.trim()}
                                aria-label="Send answer"
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
