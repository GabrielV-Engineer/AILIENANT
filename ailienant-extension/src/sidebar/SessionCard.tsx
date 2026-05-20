import { Icon } from '../shared/Icon';
import { Tooltip } from '../shared/Tooltip';
import type { Session } from '../shared/types';

interface SessionCardProps {
    session: Session;
    active: boolean;
    onOpen: (id: string) => void;
    onDelete: (id: string) => void;
}

function formatRelative(iso: string): string {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) { return ''; }
    const diff = Date.now() - then;
    const minutes = Math.floor(diff / 60_000);
    if (minutes < 1) { return 'just now'; }
    if (minutes < 60) { return `${minutes}m ago`; }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) { return `${hours}h ago`; }
    const days = Math.floor(hours / 24);
    if (days < 7) { return `${days}d ago`; }
    return new Date(iso).toLocaleDateString();
}

export function SessionCard({ session, active, onOpen, onDelete }: SessionCardProps): JSX.Element {
    return (
        <div
            className="sb-card"
            data-active={active}
            role="button"
            tabIndex={0}
            onClick={() => onOpen(session.id)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { onOpen(session.id); } }}
        >
            <div className="sb-card-title">{session.title || 'Untitled session'}</div>
            <div className="sb-card-meta">
                <span>{formatRelative(session.last_modified)}</span>
                <span>·</span>
                <span>{session.message_count} msg</span>
                <span className="sb-card-tier" data-tier={session.model_tier}>{session.model_tier}</span>
            </div>
            <Tooltip content="Delete session" side="left">
                <button
                    className="sb-card-delete"
                    onClick={(e) => { e.stopPropagation(); onDelete(session.id); }}
                    aria-label="Delete session"
                >
                    <Icon name="trash" size={14} />
                </button>
            </Tooltip>
        </div>
    );
}
