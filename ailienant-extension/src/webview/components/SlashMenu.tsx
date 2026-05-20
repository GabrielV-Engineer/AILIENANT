import { useState, useCallback, useRef, useEffect } from 'react';
import { vscode } from '../vscode_bridge';

export interface SlashCommand {
    cmd: string;
    desc: string;
    action: () => void;
}

interface Props {
    query: string;          // text after the '/'
    onClose: () => void;
    activeTaskId?: string;
    onCommandSelect?: (cmd: string) => void;
}

function buildCommands(activeTaskId: string | undefined, onClose: () => void): SlashCommand[] {
    return [
        {
            cmd: '/context',
            desc: 'Attach files to context window',
            action: () => {
                vscode.postMessage({ type: 'SUBMIT_TASK', value: '/context' });
                onClose();
            },
        },
        {
            cmd: '/context rewind',
            desc: 'Roll back to last graph checkpoint',
            action: () => {
                vscode.postMessage({ type: 'SUBMIT_TASK', value: `/context rewind ${activeTaskId ?? ''}` });
                onClose();
            },
        },
        {
            cmd: '/models',
            desc: 'Open expert model selector',
            action: () => { onClose(); },
        },
        {
            cmd: '/customize',
            desc: 'Edit persona & custom instructions',
            action: () => {
                vscode.postMessage({ type: 'SUBMIT_TASK', value: '/customize' });
                onClose();
            },
        },
        {
            cmd: '/dlq',
            desc: 'Show pending dead-letter episodes',
            action: () => {
                vscode.postMessage({ type: 'SUBMIT_TASK', value: '/dlq' });
                onClose();
            },
        },
    ];
}

export function SlashMenu({ query, onClose, activeTaskId, onCommandSelect }: Props): JSX.Element | null {
    const commands = buildCommands(activeTaskId, onClose);
    const filtered = commands.filter(c =>
        c.cmd.toLowerCase().includes(query.toLowerCase()) ||
        c.desc.toLowerCase().includes(query.toLowerCase())
    );

    const [focused, setFocused] = useState(0);
    const listRef = useRef<HTMLDivElement>(null);

    // Keyboard navigation
    useEffect(() => {
        const handler = (e: KeyboardEvent): void => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setFocused(f => (f + 1) % filtered.length);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setFocused(f => (f - 1 + filtered.length) % filtered.length);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                filtered[focused]?.action();
                onCommandSelect?.(filtered[focused]?.cmd ?? '');
            } else if (e.key === 'Escape') {
                onClose();
            }
        };
        document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [filtered, focused, onClose, onCommandSelect]);

    if (filtered.length === 0) { return null; }

    return (
        <div className="ai-slash-menu" ref={listRef} role="listbox">
            {filtered.map((c, i) => (
                <div
                    key={c.cmd}
                    className="ai-slash-item"
                    data-focused={i === focused ? 'true' : 'false'}
                    role="option"
                    aria-selected={i === focused}
                    onMouseEnter={() => setFocused(i)}
                    onClick={() => { c.action(); onCommandSelect?.(c.cmd); }}
                >
                    <span className="ai-slash-item-cmd">{c.cmd}</span>
                    <span className="ai-slash-item-desc">{c.desc}</span>
                </div>
            ))}
        </div>
    );
}

// Hook: detect if input starts with '/'
export function useSlashDetect(value: string): { slashActive: boolean; slashQuery: string } {
    const match = /^\/(.*)/.exec(value);
    return {
        slashActive: match !== null,
        slashQuery:  match ? match[1] : '',
    };
}
