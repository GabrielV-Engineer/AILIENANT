/**
 * Active Task Header — sticky prompt-preservation card pinned above the chat.
 *
 * Prompt + Stop only (13.1.9). The loader glyph, live status text and elapsed
 * clock used to live here too, mirroring AgentTimeline's own latest-row label
 * "so the two surfaces never disagree" — but two surfaces that must be kept
 * in sync by convention are two sources of truth, and a user reading both at
 * once experiences it as disagreement waiting to happen, not confirmation.
 * The Glass-Box Timeline's own live loader row (`AgentTimeline.tsx`) is now
 * the ONLY place "what's happening right now" is shown; this header keeps
 * only what the timeline can't: the submitted prompt itself (DEBT-058) and a
 * Stop control that never scrolls out of view. On completion it still
 * collapses to a one-line summary (check + truncated prompt) that stays
 * pinned until dismissed or replaced by a new submit.
 *
 * The prompt now renders in the bar at all times (done or active) rather than
 * being swapped for status text while streaming — which is also why the
 * separate "full prompt" block below the bar (needed before, when the bar was
 * occupied by the status label) is gone too: showing the same prompt twice,
 * once truncated and once expanded, would be exactly the redundancy this
 * simplification exists to remove.
 *
 * Purely presentational: all state lives in the memory-only chat store and is
 * fed in as props.
 */
import { Icon } from '../../shared/Icon';

interface Props {
    /** The submitted prompt text to preserve on screen. */
    prompt: string;
    /**
     * True for the whole turn — including node execution, tool calls, and an
     * interrupt()/resume pause with no tokens yet, not just token delivery
     * (expanded while true; collapses once the turn settles).
     */
    isTurnActive: boolean;
    /** Abort the in-flight turn (wired to the existing Stop path). */
    onCancel: () => void;
    /** Clear the header (only offered once the turn has settled). */
    onDismiss: () => void;
}

export function ActiveTaskHeader({
    prompt, isTurnActive, onCancel, onDismiss,
}: Props): JSX.Element {
    const done = !isTurnActive;

    return (
        <div
            className="ws-active-task"
            data-done={done ? 'true' : 'false'}
            role="status"
            aria-live="polite"
        >
            <div className="ws-active-task-bar">
                {done && (
                    <Icon name="check-circle" size={15} className="ws-active-task-icon ws-active-task-check" />
                )}

                <span className="ws-active-task-prompt" title={prompt}>{prompt}</span>

                {done ? (
                    <button
                        type="button"
                        className="ws-active-task-btn"
                        onClick={onDismiss}
                        aria-label="Dismiss task summary"
                    >
                        <Icon name="x" size={13} />
                    </button>
                ) : (
                    <button
                        type="button"
                        className="ws-active-task-btn ws-active-task-cancel"
                        onClick={onCancel}
                    >
                        <Icon name="square" size={10} />
                        <span>Cancel</span>
                    </button>
                )}
            </div>
        </div>
    );
}
