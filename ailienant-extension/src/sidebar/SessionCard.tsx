import { useState, useRef, useEffect, useCallback } from 'react';
import { Icon } from '../shared/Icon';
import { Tooltip } from '../shared/Tooltip';
import type { Session } from '../shared/types';

interface SessionCardProps {
    session: Session;
    active: boolean;
    logoUri: string;
    onOpen: (id: string) => void;
    onDelete: (id: string) => void;
    onRename: (id: string, title: string) => void;
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

export function SessionCard({
    session, active, logoUri, onOpen, onDelete, onRename,
}: SessionCardProps): JSX.Element {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(session.title);
    const inputRef = useRef<HTMLInputElement>(null);
    const openTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(() => () => {
        if (openTimerRef.current) { clearTimeout(openTimerRef.current); }
    }, []);

    useEffect(() => {
        if (editing && inputRef.current) {
            inputRef.current.focus();
            inputRef.current.select();
        }
    }, [editing]);

    useEffect(() => {
        if (!editing) { setDraft(session.title); }
    }, [session.title, editing]);

    const commit = (): void => {
        const next = draft.trim();
        if (next && next !== session.title) {
            onRename(session.id, next);
        } else {
            setDraft(session.title);
        }
        setEditing(false);
    };

    const cancel = (): void => {
        setDraft(session.title);
        setEditing(false);
    };

    // Delay open so a double-click can cancel it before VS Code shifts focus.
    const handleCardClick = useCallback(() => {
        if (editing) { return; }
        if (openTimerRef.current) { clearTimeout(openTimerRef.current); }
        openTimerRef.current = setTimeout(() => {
            openTimerRef.current = null;
            onOpen(session.id);
        }, 220);
    }, [editing, onOpen, session.id]);

    const handleTitleDoubleClick = useCallback((e: React.MouseEvent) => {
        e.stopPropagation();
        if (openTimerRef.current) { clearTimeout(openTimerRef.current); openTimerRef.current = null; }
        setEditing(true);
    }, []);

    const displayTitle = session.title.trim() || 'Untitled';
    const isUntitled = !session.title.trim();

    return (
        <div
            className="sb-card"
            data-active={active}
            role="button"
            tabIndex={0}
            onClick={handleCardClick}
            onKeyDown={(e) => {
                if (!editing && (e.key === 'Enter' || e.key === ' ')) {
                    e.preventDefault();
                    if (openTimerRef.current) { clearTimeout(openTimerRef.current); }
                    onOpen(session.id);
                }
            }}
        >
            <div className="sb-card-row">
                {isUntitled && logoUri && (
                    <img src={logoUri} alt="" className="sb-card-logo" aria-hidden />
                )}
                {editing ? (
                    <input
                        ref={inputRef}
                        className="sb-card-title-input"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onBlur={commit}
                        onKeyDown={(e) => {
                            e.stopPropagation();
                            if (e.key === 'Enter') { commit(); }
                            else if (e.key === 'Escape') { cancel(); }
                        }}
                        onClick={(e) => e.stopPropagation()}
                    />
                ) : (
                    <span
                        className="sb-card-title"
                        data-untitled={isUntitled}
                        onDoubleClick={handleTitleDoubleClick}
                        title="Double-click to rename"
                    >
                        {displayTitle}
                    </span>
                )}
            </div>
            <div className="sb-card-meta">
                <span>{formatRelative(session.last_modified)}</span>
                {session.message_count > 0 && (
                    <>
                        <span className="sb-card-sep">·</span>
                        <span>{session.message_count} msg</span>
                    </>
                )}
                {/* Phase 7.12 — removed dead `model_tier` badge (hardcoded 'medium'
                    at session creation; a meaningless static artifact). */}
            </div>
            <Tooltip content="Delete session" side="left">
                <button
                    className="sb-card-delete"
                    onClick={(e) => { e.stopPropagation(); onDelete(session.id); }}
                    aria-label="Delete session"
                >
                    <Icon name="trash" size={13} />
                </button>
            </Tooltip>
        </div>
    );
}
