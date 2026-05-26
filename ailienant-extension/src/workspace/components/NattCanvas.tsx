import { useState, useEffect, useRef } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { HITLInterventionCard, type HITLIntervention } from './HITLInterventionCard';
import { NattPromptBar } from './NattPromptBar';
import { MarkdownRenderer } from './MarkdownRenderer';
import type { ParserState as MdParserState } from '../utils/StreamingMarkdownParser';

interface NattMessage {
    role: 'natt' | 'user';
    content: string;
    streaming?: boolean;
    // Phase 7.11.5 — incremental markdown parser state. Live only while
    // streaming; cleared on `server_natt_stream_end`.
    parserState?: MdParserState;
}

interface AttachedItem { id: string; path: string; kind: 'file' | 'directory'; }

interface Props {
    nattName: string;
    messages: NattMessage[];
    pendingIntervention?: HITLIntervention;
    disabled?: boolean;
    nattAttachedItems: AttachedItem[];
    onNattRemoveAttached: (id: string) => void;
    onClose: () => void;
    onResolveIntervention: (approvalId: string) => void;
    onSendMessage: (text: string) => void;
}

export function NattCanvas({
    nattName, messages, pendingIntervention, disabled,
    nattAttachedItems, onNattRemoveAttached,
    onClose, onResolveIntervention, onSendMessage,
}: Props): JSX.Element {
    const bodyRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
    }, [messages, pendingIntervention]);

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

            <div className="ws-natt-body" ref={bodyRef}>
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
                            Ask a question below, or wait — they'll<br />
                            surface here when a decision is needed.
                        </div>
                    </div>
                )}

                {messages.map((m, i) => (
                    <div key={i} className="ws-natt-msg" data-role={m.role}>
                        {m.role === 'natt' && <Icon name="bot" size={14} color="var(--accent-primary)" />}
                        <div className="ws-natt-msg-content">
                            {m.role === 'natt' ? (
                                // Phase 7.11.5 — anti-flicker markdown rendering.
                                <MarkdownRenderer
                                    content={m.content}
                                    parserState={m.parserState}
                                    streaming={!!m.streaming}
                                />
                            ) : (
                                m.content
                            )}
                        </div>
                    </div>
                ))}
            </div>

            {nattAttachedItems.length > 0 && (
                <div className="ws-attached-bar">
                    {nattAttachedItems.map(item => {
                        const label = item.path.split(/[/\\]/).pop() ?? item.path;
                        return (
                            <div key={item.id} className="ws-attached-chip" title={item.path}>
                                <Icon name={item.kind === 'directory' ? 'folder' : 'file'} size={11} />
                                <span>{label}</span>
                                <button
                                    className="ws-attached-chip-remove"
                                    aria-label={`Remove ${label}`}
                                    onClick={() => onNattRemoveAttached(item.id)}
                                >
                                    <Icon name="x" size={10} />
                                </button>
                            </div>
                        );
                    })}
                </div>
            )}
            <NattPromptBar
                nattName={nattName}
                disabled={disabled}
                onSubmit={onSendMessage}
            />
        </aside>
    );
}
