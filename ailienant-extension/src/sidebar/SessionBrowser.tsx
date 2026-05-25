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

interface BootAttrs {
    logoUri: string;
}

export function SessionBrowser({ boot }: { boot: BootAttrs }): JSX.Element {
    const [sessions, setSessions] = useState<Session[]>([]);
    const [query, setQuery] = useState('');
    const [activeId, setActiveId] = useState<string | null>(null);

    useEffect(() => {
        const onMessage = (e: MessageEvent<ExtToSidebarMessage>): void => {
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
        return sessions.filter(s =>
            (s.title || 'Untitled').toLowerCase().includes(q)
        );
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

    const onRename = useCallback((id: string, title: string) => {
        vscode.postMessage({ type: 'RENAME_SESSION', session_id: id, title });
    }, []);

    return (
        <div className="sb-layout">
            {/* Zone 1: brand header */}
            <header className="sb-brand">
                <span className="sb-brand-name">AILIENANT</span>
            </header>

            {/* Zone 2: action bar */}
            <div className="sb-actions">
                <Tooltip content="Start a new AILIENANT session in a full-width editor tab" side="bottom">
                    <button
                        className="ai-btn sb-new-btn"
                        data-variant="primary"
                        onClick={onNew}
                        aria-label="New session"
                    >
                        <Icon name="plus" size={15} />
                        <span>New Session</span>
                    </button>
                </Tooltip>
                <div className="sb-search-wrap">
                    <Icon name="search" size={13} className="sb-search-icon" />
                    <input
                        className="sb-search-input"
                        type="text"
                        placeholder="Search sessions"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        aria-label="Search sessions"
                    />
                </div>
            </div>

            {/* Zone 3: session list */}
            <SessionList
                sessions={filtered}
                activeId={activeId}
                logoUri={boot.logoUri}
                onOpen={onOpen}
                onDelete={onDelete}
                onRename={onRename}
            />
        </div>
    );
}
