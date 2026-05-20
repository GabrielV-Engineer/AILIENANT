import { useState, useCallback, useRef } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { CommandPalette, useSlashDetect } from './CommandPalette';
import { ContextOverlay } from './ContextOverlay';

interface Props {
    disabled: boolean;
    placeholder?: string;
    activeTaskId?: string;
    isStreaming: boolean;
    onSubmit: (text: string) => void;
    onAbort: () => void;
}

export function PromptBar({
    disabled, placeholder, activeTaskId, isStreaming, onSubmit, onAbort,
}: Props): JSX.Element {
    const [value, setValue] = useState('');
    const [contextOpen, setContextOpen] = useState(false);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { slashActive, slashQuery } = useSlashDetect(value);

    const submit = useCallback(() => {
        const text = value.trim();
        if (!text || disabled) { return; }
        onSubmit(text);
        setValue('');
    }, [value, disabled, onSubmit]);

    const onKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey && !slashActive) {
            e.preventDefault();
            submit();
        }
    }, [submit, slashActive]);

    return (
        <div className="ws-prompt">
            {contextOpen && (
                <ContextOverlay onClose={() => setContextOpen(false)} />
            )}
            {slashActive && (
                <CommandPalette
                    query={slashQuery}
                    activeTaskId={activeTaskId}
                    onClose={() => setValue('')}
                    onCommandSelect={(cmd) => setValue(cmd + ' ')}
                />
            )}
            <div className="ws-prompt-row">
                <Tooltip content="Attach files, terminal output, or directories to the prompt context">
                    <button
                        className="ws-prompt-context"
                        onClick={() => setContextOpen(v => !v)}
                        aria-label="Add context"
                        disabled={disabled}
                    >
                        <Icon name="plus" size={16} />
                    </button>
                </Tooltip>
                <textarea
                    ref={textareaRef}
                    className="ws-prompt-input"
                    rows={2}
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={onKey}
                    placeholder={placeholder ?? 'Message AILIENANT… (Enter to send, Shift+Enter newline, / for commands)'}
                    disabled={disabled}
                    aria-label="Prompt input"
                />
                {isStreaming ? (
                    <Tooltip content="Abort current task" side="left">
                        <button
                            className="ai-btn"
                            data-variant="danger"
                            onClick={onAbort}
                            aria-label="Abort task"
                        >
                            <Icon name="x" size={16} />
                        </button>
                    </Tooltip>
                ) : (
                    <Tooltip content="Send prompt (Enter)" side="left">
                        <button
                            className="ai-btn"
                            data-variant="primary"
                            onClick={submit}
                            disabled={disabled || !value.trim()}
                            aria-label="Send"
                        >
                            <Icon name="send" size={16} />
                        </button>
                    </Tooltip>
                )}
            </div>
        </div>
    );
}
