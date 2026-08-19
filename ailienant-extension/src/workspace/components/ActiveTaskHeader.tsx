/**
 * Active Task Header — sticky prompt-preservation card pinned above the chat.
 *
 * While a turn streams it shows an animated "Working…" indicator, a live elapsed
 * clock, the submitted prompt (clamped), and a Cancel affordance. On completion it
 * collapses to a one-line summary (check + truncated prompt + frozen elapsed) that
 * stays pinned until the user dismisses it or submits a new task — so the request
 * currently on screen is never lost in a long transcript (closes DEBT-058).
 *
 * Purely presentational: all state lives in the memory-only chat store and is fed
 * in as props. The elapsed clock reuses the ReasoningStream idiom — a 1s interval
 * while streaming, frozen on the streaming→false transition.
 */
import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { ReasoningGlyph } from './ReasoningGlyph';

interface Props {
    /** The submitted prompt text to preserve on screen. */
    prompt: string;
    /** Submit timestamp (`Date.now()`) — drives the elapsed clock. */
    startedAt: number;
    /**
     * True for the whole turn — including node execution, tool calls, and an
     * interrupt()/resume pause with no tokens yet, not just token delivery
     * (expanded while true; collapses once the turn settles). Wider than the
     * old token-only `isStreaming` signal, which left this header (and the
     * Stop button) looking idle during exactly the gap a "thinking" turn
     * spends with no tokens on the wire.
     */
    isTurnActive: boolean;
    /**
     * The latest real activity narration for this turn (e.g. "Reading
     * `foo.py`", "Running `pytest`") — the same text AgentTimeline rows show,
     * so the two surfaces never disagree. Falls back to a generic "Working…"
     * before the first marker arrives; never a fabricated status verb.
     */
    statusLabel?: string;
    /** Abort the in-flight turn (wired to the existing Stop path). */
    onCancel: () => void;
    /** Clear the header (only offered once the turn has settled). */
    onDismiss: () => void;
}

/** `m:ss` elapsed formatting. */
function formatElapsed(ms: number): string {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const mins = Math.floor(totalSec / 60);
    const secs = totalSec % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

export function ActiveTaskHeader({
    prompt, startedAt, isTurnActive, statusLabel, onCancel, onDismiss,
}: Props): JSX.Element {
    const [liveMs, setLiveMs] = useState<number>(() => Date.now() - startedAt);
    // The elapsed value is frozen once the turn settles so the collapsed
    // summary shows a stable total instead of a clock that keeps ticking.
    const frozenRef = useRef<number | null>(null);

    useEffect(() => {
        if (!isTurnActive) {
            if (frozenRef.current === null) { frozenRef.current = Date.now() - startedAt; }
            return;
        }
        // A fresh turn (new startedAt) re-arms the clock.
        frozenRef.current = null;
        setLiveMs(Date.now() - startedAt);
        const id = window.setInterval(() => setLiveMs(Date.now() - startedAt), 1000);
        return () => window.clearInterval(id);
    }, [isTurnActive, startedAt]);

    const done = !isTurnActive;
    const shownMs = done ? (frozenRef.current ?? (Date.now() - startedAt)) : liveMs;

    return (
        <div
            className="ws-active-task"
            data-done={done ? 'true' : 'false'}
            role="status"
            aria-live="polite"
        >
            <div className="ws-active-task-bar">
                {done
                    ? <Icon name="check-circle" size={15} className="ws-active-task-icon ws-active-task-check" />
                    : <span className="ws-active-task-icon"><ReasoningGlyph size={16} /></span>}

                {done
                    ? <span className="ws-active-task-prompt" title={prompt}>{prompt}</span>
                    : <span className="ws-active-task-status">{statusLabel ?? 'Working…'}</span>}

                <span className="ws-active-task-elapsed" aria-label="Elapsed time">
                    {formatElapsed(shownMs)}
                </span>

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

            {/* Expanded: the full prompt sits below the control bar, clamped to two lines. */}
            {!done && (
                <div className="ws-active-task-prompt-full" title={prompt}>{prompt}</div>
            )}
        </div>
    );
}
