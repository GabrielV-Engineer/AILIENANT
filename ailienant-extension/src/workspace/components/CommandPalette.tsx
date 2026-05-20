import { useState, useEffect, useCallback } from 'react';
import { vscode } from '../vscode_bridge';

export interface SlashCommand {
    cmd: string;
    desc: string;
}

interface Props {
    query: string;
    activeTaskId?: string;
    onClose: () => void;
    onCommandSelect?: (cmd: string) => void;
}

function buildCommands(): SlashCommand[] {
    return [
        { cmd: '/context',        desc: 'Attach files, terminal output, or directories to the context window' },
        { cmd: '/context rewind', desc: 'Roll back the agent graph to its last checkpoint' },
        { cmd: '/explain',        desc: 'Generate an architectural context map for the current selection' },
        { cmd: '/patch',          desc: 'Invoke the VFS staging arena for diff validation' },
        { cmd: '/models',         desc: 'Open the expert model selector popover' },
        { cmd: '/customize',      desc: 'Edit persona and custom instructions' },
        { cmd: '/dlq',            desc: 'List pending dead-letter recovery episodes' },
    ];
}

export function CommandPalette({ query, activeTaskId, onClose, onCommandSelect }: Props): JSX.Element | null {
    const all = buildCommands();
    const q = query.toLowerCase();
    const filtered = all.filter(c => c.cmd.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q));
    const [focused, setFocused] = useState(0);

    useEffect(() => { setFocused(0); }, [query]);

    const execute = useCallback((cmd: SlashCommand) => {
        if (cmd.cmd === '/context rewind') {
            vscode.postMessage({ type: 'SUBMIT_TASK', value: `/context rewind ${activeTaskId ?? ''}` });
        } else if (cmd.cmd === '/models' || cmd.cmd === '/customize') {
            // Display-only commands; close palette
        } else {
            vscode.postMessage({ type: 'SUBMIT_TASK', value: cmd.cmd });
        }
        onCommandSelect?.(cmd.cmd);
        onClose();
    }, [activeTaskId, onCommandSelect, onClose]);

    useEffect(() => {
        const onKey = (e: KeyboardEvent): void => {
            if (filtered.length === 0) { return; }
            if (e.key === 'ArrowDown') { e.preventDefault(); setFocused(f => (f + 1) % filtered.length); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setFocused(f => (f - 1 + filtered.length) % filtered.length); }
            else if (e.key === 'Enter') { e.preventDefault(); execute(filtered[focused]); }
            else if (e.key === 'Escape') { onClose(); }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [filtered, focused, execute, onClose]);

    if (filtered.length === 0) { return null; }

    return (
        <div className="ws-palette" role="listbox" aria-label="Command palette">
            <div className="ws-palette-hint">Command palette · ↑↓ navigate · Enter to run · Esc to close</div>
            {filtered.map((c, i) => (
                <button
                    key={c.cmd}
                    className="ws-palette-item"
                    data-focused={i === focused ? 'true' : 'false'}
                    role="option"
                    aria-selected={i === focused}
                    onMouseEnter={() => setFocused(i)}
                    onClick={() => execute(c)}
                >
                    <span className="ws-palette-cmd">{c.cmd}</span>
                    <span className="ws-palette-desc">{c.desc}</span>
                </button>
            ))}
        </div>
    );
}

export function useSlashDetect(value: string): { slashActive: boolean; slashQuery: string } {
    const match = /^\/(.*)/.exec(value);
    return { slashActive: match !== null, slashQuery: match ? match[1] : '' };
}
