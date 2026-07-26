/**
 * Inline reasoning stream — the model's thinking, coupled into the chat flow.
 *
 * Not a boxed accordion: a quiet, borderless line (the same idiom as the
 * pipeline trace) with an animated infinity glyph on the left. While the model
 * reasons the prose streams live and the block is expanded; the moment the
 * answer begins the parent freezes the clock and the block collapses to a
 * re-expandable "Reasoned for Ns" summary. Provenance (native vs simulated) is
 * intentionally not surfaced — the trace reads identically either way. Rendered
 * the same in the main chat and the analyst pane so the two never diverge.
 *
 * The reasoning text is rendered through `MarkdownRenderer` (React nodes, never
 * `dangerouslySetInnerHTML`); it is display-only and never re-enters the loop.
 */
import { memo, useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { MarkdownRenderer } from './MarkdownRenderer';
import { ReasoningGlyph } from './ReasoningGlyph';
import type { ReasoningSource } from '../utils/thinkingReducer';

interface Props {
    thinking: string;
    tokens: number;
    startedAt?: number;
    /** Frozen elapsed (ms) once the answer begins; undefined while reasoning. */
    elapsedMs?: number;
    open: boolean;
    /** True while the turn is still streaming. */
    streaming: boolean;
    /** Provenance of the trace; retained on the contract but not surfaced in the UI. */
    source?: ReasoningSource;
    onToggle: () => void;
}

function ReasoningStreamImpl({
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
        ? `Reasoning… ${tokens} ${tokens === 1 ? 'token' : 'tokens'} · ${secs}s`
        : `Reasoned for ${secs}s`;

    return (
        <div className="ws-reason" data-open={open ? 'true' : 'false'} data-active={active ? 'true' : 'false'}>
            <button type="button" className="ws-reason-header" onClick={onToggle} aria-expanded={open}>
                <ReasoningGlyph size={16} still={!active} />
                <span className="ws-reason-label">{label}</span>
                <Icon
                    name={open ? 'chevron-down' : 'chevron-right'}
                    size={12}
                    className="ws-reason-chevron"
                />
            </button>
            {open && (
                <div className="ws-reason-body" role="region">
                    <MarkdownRenderer content={thinking} parserState={undefined} streaming={streaming} />
                </div>
            )}
        </div>
    );
}

/**
 * Memoised so unrelated re-renders don't re-scan the reasoning text. Re-renders
 * only when a visible input changes. `source` is deliberately absent — it no
 * longer drives any visible output.
 */
export const ReasoningStream = memo(ReasoningStreamImpl, (a, b) =>
    a.thinking === b.thinking &&
    a.tokens === b.tokens &&
    a.elapsedMs === b.elapsedMs &&
    a.open === b.open &&
    a.streaming === b.streaming &&
    a.startedAt === b.startedAt,
);
