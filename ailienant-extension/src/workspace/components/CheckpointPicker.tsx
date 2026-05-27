/**
 * Phase 7.11.8 (ADR-706 §4.5g) — CheckpointPicker overlay (Time-Travel).
 *
 * Opened from the ``/context rewind`` palette item. Shows the entire
 * checkpoint chain of the active session as a keyboard-navigable list:
 *
 *   1.  Turn 1 · 12:04:13 — "fix the parser"
 *   2.  Turn 2 · 12:05:01 — "add a docstring"
 *   3. ⏹ aborted · 12:06:30 — "rewrite the helper"
 *
 * ↑↓ moves the highlight, Enter selects (dispatches BRANCH_FROM_CHECKPOINT),
 * Esc closes. Click also works.
 *
 * Security note (ADR-705): the data shown here is ONLY opaque IDs +
 * timestamps + ``termination_reason``. No serialized state, no
 * ``proposed_content``, no model output. The list is fetched via REST
 * ``GET /api/v1/sessions/{id}/checkpoints`` and forwarded to the webview as
 * ``CHECKPOINTS_LIST`` by the host.
 */
import { memo, useCallback, useEffect, useRef, useState } from 'react';

export interface CheckpointEntry {
    checkpoint_id: string;
    parent_id: string | null;
    promoted_at: number;
    termination_reason: string | null;
    turn_index: number;
}

export interface CheckpointPickerProps {
    /** Pre-fetched list (the host's REST round-trip already resolved). */
    entries: CheckpointEntry[];
    /** Closes the overlay without picking. */
    onCancel: () => void;
    /** Fires BRANCH_FROM_CHECKPOINT when the user picks a row. */
    onPick: (entry: CheckpointEntry) => void;
}

function formatTime(monotonic: number): string {
    // `promoted_at` is the result of `time.monotonic()` on the server — it is
    // NOT a wall-clock time and cannot be turned into a Date. We render it as
    // a relative "Δ since first" string so the UI is still meaningful.
    return monotonic.toFixed(1) + 's';
}

export const CheckpointPicker = memo(function CheckpointPicker(
    props: CheckpointPickerProps,
): JSX.Element {
    const { entries, onCancel, onPick } = props;
    const [activeIdx, setActiveIdx] = useState<number>(
        entries.length > 0 ? entries.length - 1 : -1,
    );
    const rootRef = useRef<HTMLDivElement | null>(null);

    // Keyboard navigation. Bound to the overlay root so it works even when
    // the underlying chat retains text-input focus.
    const onKey = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            onCancel();
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIdx((i) => Math.min(entries.length - 1, i + 1));
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIdx((i) => Math.max(0, i - 1));
            return;
        }
        if (e.key === 'Enter') {
            e.preventDefault();
            const target = entries[activeIdx];
            if (target) { onPick(target); }
        }
    }, [activeIdx, entries, onPick, onCancel]);

    // Steal focus so the keyboard handler above can fire immediately without
    // the user having to click the overlay first.
    useEffect(() => {
        rootRef.current?.focus();
    }, []);

    if (entries.length === 0) {
        return (
            <div className="ws-checkpoint-picker" role="dialog" aria-label="Time-travel checkpoints"
                 ref={rootRef} tabIndex={-1} onKeyDown={onKey}>
                <div className="ws-checkpoint-picker-header">Time-travel — pick a checkpoint</div>
                <div className="ws-checkpoint-picker-empty">
                    No checkpoints yet for this session. Complete at least one
                    coding turn — the next stream-end will produce a branchable
                    checkpoint.
                </div>
                <button type="button" className="ws-checkpoint-picker-cancel"
                        onClick={onCancel} aria-label="Close">Close (Esc)</button>
            </div>
        );
    }

    return (
        <div className="ws-checkpoint-picker" role="dialog" aria-label="Time-travel checkpoints"
             ref={rootRef} tabIndex={-1} onKeyDown={onKey}>
            <div className="ws-checkpoint-picker-header">
                Time-travel — pick a checkpoint to branch from
            </div>
            <ul className="ws-checkpoint-picker-list" role="listbox">
                {entries.map((e, i) => {
                    const isAbort = e.termination_reason === 'user_abort';
                    const active = i === activeIdx;
                    return (
                        <li
                            key={e.checkpoint_id}
                            role="option"
                            aria-selected={active}
                            data-active={active}
                            data-abort={isAbort}
                            className="ws-checkpoint-picker-row"
                            onClick={() => { setActiveIdx(i); onPick(e); }}
                            onMouseEnter={() => setActiveIdx(i)}
                        >
                            <span className="ws-checkpoint-picker-icon" aria-hidden="true">
                                {isAbort ? '⏹' : '◷'}
                            </span>
                            <span className="ws-checkpoint-picker-turn">
                                Turn {e.turn_index + 1}
                            </span>
                            {isAbort && (
                                <span className="ws-checkpoint-picker-badge">aborted</span>
                            )}
                            <span className="ws-checkpoint-picker-ts">
                                {formatTime(e.promoted_at)}
                            </span>
                            <span className="ws-checkpoint-picker-id" aria-hidden="true">
                                {e.checkpoint_id.slice(0, 8)}
                            </span>
                        </li>
                    );
                })}
            </ul>
            <div className="ws-checkpoint-picker-footer">
                <kbd>↑↓</kbd> navigate · <kbd>Enter</kbd> branch · <kbd>Esc</kbd> close
            </div>
        </div>
    );
});
