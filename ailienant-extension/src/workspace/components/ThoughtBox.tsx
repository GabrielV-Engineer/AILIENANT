/**
 * Phase 9 (ADR-707) — Native Thinking "Thought Box".
 *
 * A collapsible accordion rendered inside an assistant bubble that shows the
 * model's raw reasoning stream in real time, inspired by Claude Code's thinking
 * UX. It auto-expands while reasoning tokens arrive and is auto-collapsed by the
 * `server_token_chunk` handler the moment the first answer token lands.
 *
 * Chronometrics: while reasoning is in flight the elapsed clock ticks locally
 * (component state — deliberately NOT in Zustand, to avoid a store write per
 * frame). Once the parent freezes `elapsedMs` (first answer token), the live
 * clock stops and the header switches to the summary label.
 *
 * Security: the reasoning text is rendered through `MarkdownRenderer`, which
 * emits React nodes (never `dangerouslySetInnerHTML`), so untrusted scratchpad
 * content cannot inject markup. It is display-only and never re-enters the
 * agent loop (see Workspace.tsx — thinking is excluded from PERSIST_TRANSCRIPT).
 */
import { memo, useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { MarkdownRenderer } from './MarkdownRenderer';

interface Props {
    thinking: string;
    tokens: number;
    startedAt?: number;
    /** Frozen elapsed (ms) once the answer begins; undefined while reasoning. */
    elapsedMs?: number;
    open: boolean;
    /** True while the assistant turn is still streaming. */
    streaming: boolean;
    onToggle: () => void;
}

function ThoughtBoxImpl({
    thinking, tokens, startedAt, elapsedMs, open, streaming, onToggle,
}: Props): JSX.Element {
    const active = elapsedMs === undefined && streaming;
    const [liveMs, setLiveMs] = useState(0);

    useEffect(() => {
        if (!active || startedAt === undefined) { return; }
        const id = window.setInterval(() => {
            setLiveMs(Math.max(0, performance.now() - startedAt));
        }, 100);
        return () => window.clearInterval(id);
    }, [active, startedAt]);

    const shownMs = elapsedMs ?? (startedAt !== undefined ? liveMs : 0);
    const secs = (shownMs / 1000).toFixed(1);
    const label = active
        ? `Thinking… ${tokens} ${tokens === 1 ? 'token' : 'tokens'} · ${secs}s`
        : `Thought for ${secs}s`;

    return (
        <div
            className="ws-thought-box"
            data-open={open ? 'true' : 'false'}
            data-active={active ? 'true' : 'false'}
        >
            <button
                type="button"
                className="ws-thought-header"
                onClick={onToggle}
                aria-expanded={open}
            >
                <Icon name="chevron-right" size={12} className="ws-thought-caret" />
                <Icon name="brain" size={13} className="ws-thought-icon" />
                <span className="ws-thought-label">{label}</span>
            </button>
            {open && (
                <div className="ws-thought-body" role="region">
                    <MarkdownRenderer content={thinking} parserState={undefined} streaming={streaming} />
                </div>
            )}
        </div>
    );
}

/**
 * Memoised so unrelated bubble re-renders don't re-scan the reasoning text.
 * Re-renders only when the visible inputs change (thinking grows, the accordion
 * toggles, the clock freezes, or streaming flips).
 */
export const ThoughtBox = memo(ThoughtBoxImpl, (a, b) =>
    a.thinking === b.thinking &&
    a.tokens === b.tokens &&
    a.elapsedMs === b.elapsedMs &&
    a.open === b.open &&
    a.streaming === b.streaming &&
    a.startedAt === b.startedAt,
);
