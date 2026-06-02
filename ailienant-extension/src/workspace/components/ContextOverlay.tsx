import { useState } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

type Kind = 'file' | 'terminal';

interface Tab { id: Kind; label: string; icon: IconName; }
const TABS: Tab[] = [
    { id: 'file',     label: 'Files',    icon: 'file' },
    { id: 'terminal', label: 'Terminal', icon: 'terminal' },
];

interface Props {
    onClose: () => void;
}

export function ContextOverlay({ onClose }: Props): JSX.Element {
    const [active, setActive] = useState<Kind>('file');
    const [payload, setPayload] = useState('');

    const submitTerminal = (): void => {
        const value = payload.trim();
        if (!value) { return; }
        vscode.postMessage({ type: 'ATTACH_CONTEXT', kind: 'terminal', payload: value });
        setPayload('');
        onClose();
    };

    const browseFiles = (): void => {
        vscode.postMessage({ type: 'PICK_FILES' });
        onClose();
    };

    const browseFolder = (): void => {
        vscode.postMessage({ type: 'PICK_FOLDER' });
        onClose();
    };

    return (
        <div className="ws-context-overlay ai-card" role="dialog" aria-label="Add to context">
            <div className="ws-context-head">
                <span className="ws-mode-label">Add to context</span>
                <Tooltip content="Close" side="left">
                    <button
                        className="ai-btn"
                        data-variant="ghost"
                        onClick={onClose}
                        aria-label="Close"
                    >
                        <Icon name="x" size={12} />
                    </button>
                </Tooltip>
            </div>
            <div className="ws-context-tabs">
                {TABS.map(t => (
                    <Tooltip key={t.id} content={`Attach ${t.label.toLowerCase()}`}>
                        <button
                            className="ws-context-tab"
                            data-active={active === t.id ? 'true' : 'false'}
                            onClick={() => { setActive(t.id); setPayload(''); }}
                        >
                            <Icon name={t.icon} size={12} />
                            <span>{t.label}</span>
                        </button>
                    </Tooltip>
                ))}
            </div>
            {active === 'terminal' ? (
                <>
                    <div className="ws-context-hint">
                        No terminal auto-capture — paste the output here yourself (VS Code exposes no terminal-output API).
                    </div>
                    <input
                        className="ws-context-input"
                        placeholder="paste the last ~40 lines of terminal output"
                        value={payload}
                        onChange={(e) => setPayload(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') { submitTerminal(); } }}
                        autoFocus
                    />
                    <button
                        className="ai-btn ws-context-submit"
                        data-variant="primary"
                        onClick={submitTerminal}
                        disabled={!payload.trim()}
                    >
                        <Icon name="plus" size={12} /><span>Attach</span>
                    </button>
                </>
            ) : (
                <div className="ws-context-browse-group">
                    <button className="ws-core-menu-btn ws-context-browse-btn" onClick={browseFiles}>
                        <Icon name="file" size={13} />
                        Browse files…
                    </button>
                    <button className="ws-core-menu-btn ws-context-browse-btn" onClick={browseFolder}>
                        <Icon name="folder" size={13} />
                        Browse folder…
                    </button>
                </div>
            )}
        </div>
    );
}
