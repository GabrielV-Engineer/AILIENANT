import { useState, useCallback } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { NattContextOverlay } from './NattContextOverlay';

interface Props {
    nattName: string;
    disabled?: boolean;
    onSubmit: (text: string) => void;
}

export function NattPromptBar({ nattName, disabled, onSubmit }: Props): JSX.Element {
    const [value, setValue] = useState('');
    const [contextOpen, setContextOpen] = useState(false);

    const submit = useCallback(() => {
        const t = value.trim();
        if (!t || disabled) { return; }
        onSubmit(t);
        setValue('');
    }, [value, disabled, onSubmit]);

    return (
        <div className="ws-natt-prompt">
            {contextOpen && <NattContextOverlay onClose={() => setContextOpen(false)} />}
            <Tooltip content="Attach files to Natt context">
                <button
                    className="ws-prompt-icon-btn"
                    onClick={() => setContextOpen(v => !v)}
                    aria-label="Attach files to Natt"
                    disabled={disabled}
                >
                    <Icon name="plus" size={15} />
                </button>
            </Tooltip>
            <textarea
                className="ws-natt-prompt-input"
                rows={2}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        submit();
                    }
                }}
                placeholder={`Ask ${nattName}…`}
                disabled={disabled}
                aria-label={`Message ${nattName}`}
            />
            <Tooltip content={`Send to ${nattName} (Enter)`} side="left">
                <button
                    className="ai-btn"
                    data-variant="primary"
                    onClick={submit}
                    disabled={disabled || !value.trim()}
                    aria-label={`Send to ${nattName}`}
                >
                    <Icon name="send" size={14} />
                </button>
            </Tooltip>
        </div>
    );
}
