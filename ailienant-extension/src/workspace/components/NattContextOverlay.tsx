import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { vscode } from '../vscode_bridge';

interface Props { onClose: () => void; }

export function NattContextOverlay({ onClose }: Props): JSX.Element {
    return (
        <div className="ws-context-overlay ai-card" role="dialog" aria-label="Attach to Natt">
            <div className="ws-context-head">
                <span className="ws-mode-label">Attach to Natt</span>
                <Tooltip content="Close" side="left">
                    <button className="ai-btn" data-variant="ghost" onClick={onClose} aria-label="Close">
                        <Icon name="x" size={12} />
                    </button>
                </Tooltip>
            </div>
            <div className="ws-context-browse-group">
                <button
                    className="ws-core-menu-btn ws-context-browse-btn"
                    onClick={() => { vscode.postMessage({ type: 'PICK_NATT_FILES' }); onClose(); }}
                >
                    <Icon name="file" size={13} />Browse files…
                </button>
                <button
                    className="ws-core-menu-btn ws-context-browse-btn"
                    onClick={() => { vscode.postMessage({ type: 'PICK_NATT_FOLDER' }); onClose(); }}
                >
                    <Icon name="folder" size={13} />Browse folder…
                </button>
            </div>
        </div>
    );
}
