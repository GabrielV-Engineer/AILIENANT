import { useState, useEffect, useRef } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { NattPromptBar } from './NattPromptBar';
import { MarkdownRenderer } from './MarkdownRenderer';
import { vscode } from '../vscode_bridge';
import { useWorkspaceStore, type AnalystTier } from '../workspaceStore';
import type { ParserState as MdParserState } from '../utils/StreamingMarkdownParser';

const _ANALYST_TIERS: AnalystTier[] = ['small', 'medium', 'big', 'cloud'];
const _TIER_LABEL: Record<AnalystTier, string> = {
    small: 'Fast', medium: 'Balanced', big: 'Deep', cloud: 'Cloud',
};

/**
 * Analyst answer-model picker. Lists the active BYOM preset's tiers (with their
 * model names), greying out any the preset leaves unset, and persists the choice.
 * It only changes which model writes the answer — retrieval/grounding is unaffected.
 */
function AnalystModelPicker(): JSX.Element {
    const analystTier = useWorkspaceStore(s => s.analystTier);
    const setAnalystTier = useWorkspaceStore(s => s.setAnalystTier);
    const [tiers, setTiers] = useState<Record<string, string>>({});

    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as {
                type: string;
                data?: { presets?: { id: string; tiers: Record<string, string> }[]; active_preset_id?: string | null };
            };
            if (msg.type === 'BYOM_CONFIG' && msg.data) {
                const active = msg.data.presets?.find(p => p.id === msg.data!.active_preset_id);
                const next = active?.tiers ?? {};
                setTiers(next);
                // Reset a stale persisted tier the active preset no longer defines.
                if (Object.keys(next).length > 0 && !next[analystTier]) {
                    const fallback = (['medium', 'big', 'small', 'cloud'] as AnalystTier[])
                        .find(t => next[t]);
                    if (fallback) { setAnalystTier(fallback); }
                }
            }
        };
        window.addEventListener('message', handler);
        vscode.postMessage({ type: 'GET_BYOM_CONFIG' });
        return () => window.removeEventListener('message', handler);
    }, [analystTier, setAnalystTier]);

    const known = Object.keys(tiers).length > 0;
    return (
        <Tooltip content="Analyst answer model — trade speed for depth (retrieval is unchanged)">
            <select
                className="ws-natt-model"
                value={analystTier}
                aria-label="Analyst answer model"
                onChange={e => setAnalystTier(e.target.value as AnalystTier)}
                style={{ fontSize: 11, background: 'transparent', color: 'var(--vscode-foreground)',
                         border: '1px solid var(--vscode-panel-border)', borderRadius: 4, padding: '1px 4px' }}
            >
                {_ANALYST_TIERS.map(t => (
                    <option key={t} value={t} disabled={known && !tiers[t]}>
                        {_TIER_LABEL[t]}{tiers[t] ? ` · ${tiers[t]}` : ''}
                    </option>
                ))}
            </select>
        </Tooltip>
    );
}

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
    disabled?: boolean;
    nattAttachedItems: AttachedItem[];
    onNattRemoveAttached: (id: string) => void;
    onClose: () => void;
    onSendMessage: (text: string) => void;
}

export function NattCanvas({
    nattName, messages, disabled,
    nattAttachedItems, onNattRemoveAttached,
    onClose, onSendMessage,
}: Props): JSX.Element {
    const bodyRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
    }, [messages]);

    return (
        <aside className="ws-natt">
            <header className="ws-natt-head">
                <div className="ws-natt-head-title">
                    <Icon name="bot" size={16} color="var(--accent-primary)" />
                    <span>{nattName} · Analyst</span>
                </div>
                <AnalystModelPicker />
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
                {messages.length === 0 && (
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
