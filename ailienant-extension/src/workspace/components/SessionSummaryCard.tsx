/**
 * SessionSummaryCard — the collapsible fold that replaces a run of compacted messages.
 *
 * When a session grows past the declutter threshold and the backend compacts the context
 * window, the messages before the compaction point are folded behind this single card
 * instead of scrolling forever. The fold is NON-DESTRUCTIVE: the original bubbles stay in
 * the store and are one click away via "Show original messages" — this card only changes
 * what is rendered, never the model's actual (already-compacted) context.
 *
 * Two modes:
 *   'folded'   — the default: a one-line header ("N earlier messages compacted") that
 *                expands to reveal the AI prose summary plus the reveal affordance.
 *   'revealed' — a slim bar shown above the restored transcript to fold it back.
 *
 * Purely presentational: all fold state lives in the host component and is fed in as props.
 */
import { Icon } from '../../shared/Icon';

interface Props {
    /** 'folded' shows the summary card; 'revealed' shows the collapse-again bar. */
    mode: 'folded' | 'revealed';
    /** Number of bubbles hidden behind the fold — the count the user actually sees vanish. */
    hiddenCount: number;
    /** AI prose summary of the evicted turns (empty when the backend truncated instead). */
    summaryText: string;
    /** Whether the prose body is open (folded mode only). */
    expanded: boolean;
    /** Toggle the prose body open/closed (folded mode). */
    onToggle: () => void;
    /** Restore the original bubbles into the transcript (folded mode). */
    onRevealOriginal: () => void;
    /** Fold the restored bubbles back behind the card (revealed mode). */
    onHideOriginal: () => void;
}

const bodyId = 'ws-session-summary-body';

export function SessionSummaryCard({
    mode, hiddenCount, summaryText, expanded,
    onToggle, onRevealOriginal, onHideOriginal,
}: Props): JSX.Element {
    const countLabel = `${hiddenCount} earlier message${hiddenCount === 1 ? '' : 's'}`;

    if (mode === 'revealed') {
        return (
            <div className="ws-session-summary" data-mode="revealed">
                <button
                    type="button"
                    className="ws-session-summary-head"
                    onClick={onHideOriginal}
                    aria-label={`Hide ${hiddenCount} restored messages`}
                >
                    <Icon name="chevron-down" size={15} className="ws-session-summary-chev" />
                    <span className="ws-session-summary-title">Hide {countLabel}</span>
                    <Icon name="eye-off" size={14} className="ws-session-summary-reveal-icon" />
                </button>
            </div>
        );
    }

    return (
        <div className="ws-session-summary" data-mode="folded" data-expanded={expanded ? 'true' : 'false'}>
            <button
                type="button"
                className="ws-session-summary-head"
                onClick={onToggle}
                aria-expanded={expanded}
                aria-controls={bodyId}
            >
                <Icon
                    name={expanded ? 'chevron-down' : 'chevron-right'}
                    size={15}
                    className="ws-session-summary-chev"
                />
                <span className="ws-session-summary-box" aria-hidden>📦</span>
                <span className="ws-session-summary-title">
                    {countLabel} compacted into a summary
                </span>
            </button>

            {expanded && (
                <div id={bodyId} className="ws-session-summary-body">
                    <div className="ws-session-summary-prose">
                        {summaryText.trim() || 'Context compacted to fit the window.'}
                    </div>
                    <button
                        type="button"
                        className="ws-session-summary-reveal"
                        onClick={onRevealOriginal}
                    >
                        <Icon name="eye" size={14} />
                        <span>Show original {countLabel}</span>
                    </button>
                </div>
            )}
        </div>
    );
}
