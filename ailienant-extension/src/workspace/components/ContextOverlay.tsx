import { useState } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

type Kind = 'file' | 'terminal' | 'directory';

interface Tab { id: Kind; label: string; icon: IconName; placeholder: string; }
const TABS: Tab[] = [
    { id: 'file',      label: 'Files',     icon: 'file',     placeholder: 'src/app.ts' },
    { id: 'terminal',  label: 'Terminal',  icon: 'terminal', placeholder: 'last 40 lines of npm output' },
    { id: 'directory', label: 'Directory', icon: 'folder',   placeholder: 'src/components/' },
];

interface Props {
    onClose: () => void;
    onAttach?: (kind: Kind, payload: string) => void;
}

export function ContextOverlay({ onClose, onAttach }: Props): JSX.Element {
    const [active, setActive] = useState<Kind>('file');
    const [payload, setPayload] = useState('');

    const submit = (): void => {
        const value = payload.trim();
        if (!value) { return; }
        vscode.postMessage({ type: 'ATTACH_CONTEXT', kind: active, payload: value });
        onAttach?.(active, value);
        setPayload('');
        onClose();
    };

    return (
        <div className="ws-context-overlay ai-card" role="dialog" aria-label="Attach context">
            <div className="ws-context-tabs">
                {TABS.map(t => (
                    <Tooltip key={t.id} content={`Attach ${t.label.toLowerCase()}`}>
                        <button
                            className="ws-context-tab"
                            data-active={active === t.id ? 'true' : 'false'}
                            onClick={() => setActive(t.id)}
                        >
                            <Icon name={t.icon} size={14} />
                            <span>{t.label}</span>
                        </button>
                    </Tooltip>
                ))}
                <span style={{ flex: 1 }} />
                <Tooltip content="Close" side="left">
                    <button
                        className="ai-btn"
                        data-variant="ghost"
                        onClick={onClose}
                        aria-label="Close context overlay"
                    >
                        <Icon name="x" size={14} />
                    </button>
                </Tooltip>
            </div>
            <input
                className="ai-input"
                placeholder={TABS.find(t => t.id === active)?.placeholder}
                value={payload}
                onChange={(e) => setPayload(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { submit(); } }}
                autoFocus
            />
            <div className="ws-context-actions">
                <Tooltip content="Attach to current prompt context">
                    <button
                        className="ai-btn"
                        data-variant="primary"
                        onClick={submit}
                        disabled={!payload.trim()}
                    >
                        <Icon name="plus" size={14} /><span>Attach</span>
                    </button>
                </Tooltip>
            </div>
        </div>
    );
}
