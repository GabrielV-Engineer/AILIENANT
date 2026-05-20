import { SessionCard } from './SessionCard';
import type { Session } from '../shared/types';

interface SessionListProps {
    sessions: Session[];
    activeId: string | null;
    onOpen: (id: string) => void;
    onDelete: (id: string) => void;
}

export function SessionList({ sessions, activeId, onOpen, onDelete }: SessionListProps): JSX.Element {
    if (sessions.length === 0) {
        return (
            <div className="sb-empty">
                No sessions yet.<br />
                Click <b>New Session</b> to start.
            </div>
        );
    }
    return (
        <div className="sb-list" role="list">
            {sessions.map(s => (
                <SessionCard
                    key={s.id}
                    session={s}
                    active={activeId === s.id}
                    onOpen={onOpen}
                    onDelete={onDelete}
                />
            ))}
        </div>
    );
}
