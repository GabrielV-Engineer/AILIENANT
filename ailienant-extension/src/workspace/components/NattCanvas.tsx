import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { HITLInterventionCard, type HITLIntervention } from './HITLInterventionCard';

interface NattMessage {
    role: 'natt' | 'user';
    content: string;
}

interface Props {
    nattName: string;
    messages: NattMessage[];
    pendingIntervention?: HITLIntervention;
    onClose: () => void;
    onResolveIntervention: (approvalId: string) => void;
}

export function NattCanvas({
    nattName, messages, pendingIntervention, onClose, onResolveIntervention,
}: Props): JSX.Element {
    return (
        <aside className="ws-natt">
            <header className="ws-natt-head">
                <div className="ws-natt-head-title">
                    <Icon name="bot" size={16} color="var(--accent-primary)" />
                    <span>{nattName} · Analyst</span>
                </div>
                <Tooltip content={`Close ${nattName} pane`}>
                    <button
                        className="ai-btn"
                        data-variant="ghost"
                        onClick={onClose}
                        aria-label={`Close ${nattName}`}
                    >
                        <Icon name="panel-right-close" size={16} />
                    </button>
                </Tooltip>
            </header>

            <div className="ws-natt-body">
                {pendingIntervention && (
                    <HITLInterventionCard
                        intervention={pendingIntervention}
                        nattName={nattName}
                        onResolved={onResolveIntervention}
                    />
                )}

                {messages.length === 0 && !pendingIntervention && (
                    <div className="ws-natt-empty">
                        <Icon name="sparkles" size={20} color="var(--accent-primary)" />
                        <div>
                            <strong>{nattName}</strong> is your dedicated analyst.<br />
                            They will surface here when a decision is needed,<br />
                            or you can ask a question directly.
                        </div>
                    </div>
                )}

                {messages.map((m, i) => (
                    <div key={i} className="ws-natt-msg" data-role={m.role}>
                        {m.role === 'natt' && <Icon name="bot" size={14} color="var(--accent-primary)" />}
                        <div className="ws-natt-msg-content">{m.content}</div>
                    </div>
                ))}
            </div>
        </aside>
    );
}
