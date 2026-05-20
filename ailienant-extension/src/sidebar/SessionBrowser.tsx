import { useState, useEffect, useMemo, useCallback } from 'react';
import { Icon } from '../shared/Icon';
import { Tooltip } from '../shared/Tooltip';
import { SessionList } from './SessionList';
import type { Session, ExtToSidebarMessage, SidebarToExtMessage } from '../shared/types';

interface VsCodeApi {
    postMessage(msg: SidebarToExtMessage): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

const vscode = acquireVsCodeApi();

export function SessionBrowser(): JSX.Element {
    const [sessions, setSessions] = useState<Session[]>([]);
    const [query, setQuery] = useState('');
    const [activeId, setActiveId] = useState<string | null>(null);

    useEffect(() => {
        const onMessage = (e: MessageEvent<ExtToSidebarMessage>) => {
            if (e.data.type === 'SESSIONS_UPDATED') {
                setSessions(e.data.sessions);
            }
        };
        window.addEventListener('message', onMessage);
        return () => window.removeEventListener('message', onMessage);
    }, []);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) { return sessions; }
        return sessions.filter(s => s.title.toLowerCase().includes(q));
    }, [sessions, query]);

    const onNew = useCallback(() => {
        vscode.postMessage({ type: 'NEW_SESSION' });
    }, []);

    const onOpen = useCallback((id: string) => {
        setActiveId(id);
        vscode.postMessage({ type: 'OPEN_SESSION', session_id: id });
    }, []);

    const onDelete = useCallback((id: string) => {
        vscode.postMessage({ type: 'DELETE_SESSION', session_id: id });
    }, []);

    return (
        <div className="sb-layout">
            <div className="sb-top">
                <Tooltip content="Start a new AILIENANT session in a new editor tab" side="bottom">
                    <button
                        className="ai-btn sb-new-btn"
                        data-variant="primary"
                        onClick={onNew}
                        aria-label="New session"
                    >
                        <Icon name="plus" size={16} />
                        <span>New Session</span>
                    </button>
                </Tooltip>
                <div className="sb-search-wrap">
                    <span className="sb-search-icon"><Icon name="search" size={14} /></span>
                    <input
                        className="ai-input sb-search-input"
                        type="text"
                        placeholder="Search sessions..."
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        aria-label="Search sessions"
                    />
                </div>
            </div>
            <SessionList
                sessions={filtered}
                activeId={activeId}
                onOpen={onOpen}
                onDelete={onDelete}
            />
        </div>
    );
}
