/**
 * Phase 7.11.4 (ADR-706 §4.5d) — @mention autocomplete dropdown.
 *
 * Absolute-positioned above the prompt textarea. Renders the top-N
 * `MentionItem` candidates returned by the host-side workspace path index,
 * plus a constant `@terminal` entry (stub — selecting it opens the existing
 * ContextOverlay terminal tab via `OPEN_CONTEXT_TERMINAL`).
 *
 * Keyboard handling lives in `PromptBar` so a single keydown branch decides
 * whether to send to the dropdown or to the palette/textarea. This component
 * is purely presentational + click-to-select.
 */
import { useEffect, useRef } from 'react';
import type { MentionItem } from '../../shared/config';

interface Props {
    query:     string;
    results:   MentionItem[];
    activeIdx: number;
    onSelect:  (item: MentionItem) => void;
    onHoverIdx: (idx: number) => void;
}

export function MentionDropdown(
    { query, results, activeIdx, onSelect, onHoverIdx }: Props,
): JSX.Element {
    const activeRef = useRef<HTMLButtonElement>(null);

    // Keep the active row in view when the user arrow-keys past the edge.
    useEffect(() => {
        activeRef.current?.scrollIntoView({ block: 'nearest' });
    }, [activeIdx]);

    return (
        <div className="ws-mention-dropdown" role="listbox" aria-label="Workspace mentions">
            {results.length === 0 ? (
                <div className="ws-mention-empty">
                    {query
                        ? `No workspace paths match “${query}”.`
                        : 'Type a path fragment (or pick @terminal to paste terminal output manually).'}
                </div>
            ) : (
                results.map((item, i) => (
                    <button
                        key={`${item.kind}:${item.path}`}
                        ref={i === activeIdx ? activeRef : undefined}
                        className="ws-mention-item"
                        data-active={i === activeIdx ? 'true' : 'false'}
                        type="button"
                        role="option"
                        aria-selected={i === activeIdx}
                        onClick={() => onSelect(item)}
                        onMouseEnter={() => onHoverIdx(i)}
                    >
                        <span className="ws-mention-item-kind">{item.kind}</span>
                        <span className="ws-mention-item-path">{item.path}</span>
                    </button>
                ))
            )}
        </div>
    );
}
