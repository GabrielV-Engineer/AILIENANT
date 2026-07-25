/**
 * Inline reasoning stream — the model's thinking, coupled into the chat flow.
 *
 * Not a boxed accordion: a quiet, borderless line (the same idiom as the
 * pipeline trace) with an animated infinity glyph on the left. While the model
 * reasons the prose streams live and the block is expanded; the moment the
 * answer begins the parent freezes the clock and the block collapses to a
 * re-expandable "Reasoned for Ns" summary. A small `[Native]`/`[Simulated]` tag
 * states the trace's provenance honestly. Rendered identically in the main chat
 * and the analyst pane so the two never diverge.
 *
 * The reasoning text is rendered through `MarkdownRenderer` (React nodes, never
 * `dangerouslySetInnerHTML`); it is display-only and never re-enters the loop.
 */
import { memo, useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
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
    /** Provenance of the trace; defaults to native. */
    source?: ReasoningSource;
    onToggle: () => void;
}

const _TAG_TIP: Record<ReasoningSource, string> = {
    native: 'Native reasoning — the model emits its own thinking tokens.',
    simulated:
        'Simulated reasoning — this model has no native thinking, so AILIENANT '
        + 'prompts it to reason step by step before answering.',
};

function ReasoningStreamImpl({
    thinking, tokens, startedAt, elapsedMs, open, streaming, source = 'native', onToggle,
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
                <Tooltip content={_TAG_TIP[source]}>
                    <span className="ws-reason-tag" data-source={source}>
                        {source === 'simulated' ? 'Simulated' : 'Native'}
                    </span>
                </Tooltip>
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
 * only when a visible input changes — note `source` MUST be here or the tag
 * would not update when provenance resolves.
 */
export const ReasoningStream = memo(ReasoningStreamImpl, (a, b) =>
    a.thinking === b.thinking &&
    a.tokens === b.tokens &&
    a.elapsedMs === b.elapsedMs &&
    a.open === b.open &&
    a.streaming === b.streaming &&
    a.startedAt === b.startedAt &&
    a.source === b.source,
);
