import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

interface Props {
    sessionTitle: string;
    nattName: string;
    nattOpen: boolean;
    onToggleNatt: () => void;
    logoUri: string;
}

export function WorkspaceHeader({
    sessionTitle, nattName, nattOpen, onToggleNatt, logoUri,
}: Props): JSX.Element {
    const displayTitle = sessionTitle.trim() || 'AILIENANT';
    return (
        <header className="ws-header">
            <div className="ws-header-left">
                {logoUri && <img src={logoUri} alt="AILIENANT" className="ws-logo" />}
                <span className="ws-session-title" title={displayTitle}>{displayTitle}</span>
            </div>
            <div className="ws-header-right">
                <Tooltip content={nattOpen ? `Hide ${nattName} pane` : `Open a dedicated conversation with ${nattName}`}>
                    <button
                        className="ai-btn"
                        data-variant={nattOpen ? 'primary' : 'ghost'}
                        onClick={onToggleNatt}
                        aria-label={`Talk to ${nattName}`}
                    >
                        <Icon name="bot" size={16} />
                        <span>Talk to {nattName}</span>
                    </button>
                </Tooltip>
            </div>
        </header>
    );
}
