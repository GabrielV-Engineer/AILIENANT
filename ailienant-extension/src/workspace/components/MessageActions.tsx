/**
 * Per-message action bar (Time-Travel).
 *
 * Rendered under every COMPLETED assistant ``Message`` that carries a
 * ``checkpoint_id`` (i.e. its turn produced an L2-promoted snapshot the user
 * can fork from). The action is an icon-only circular "rewind to here"
 * control; the component is structured so future actions (Regenerate, Edit +
 * resend) can slot in alongside without disturbing the existing button.
 *
 * The rewind glyph (⟲) reads as time-travel rather than a generic git branch:
 * the user's mental model is "go back to this point", and the underlying
 * fork-from-checkpoint keeps the original conversation untouched — a
 * non-destructive rewind, surfaced as such.
 *
 * UX — two-step confirmation, identical to the ToolChip retry flow. A bare
 * icon cannot safely signal a destructive-ish confirm, so the confirm step
 * reveals a textual "Confirm?" label:
 *     first click  → button reveals "Confirm?" with a pulse animation
 *     second click → dispatches BRANCH_FROM_CHECKPOINT to the host
 *     3 s idle     → reverts to idle (no stuck Confirm? state)
 *
 * Abort-savepoint variant — when the source checkpoint was captured by the
 * emergency-savepoint path (``termination_reason === "user_abort"``), the icon
 * switches to ⏹ and the tooltip + aria-label make it clear the rewind starts
 * from an aborted state. Powerful UX: "go back to before I clicked Stop and
 * try a different path".
 *
 * Security note (ADR-705): the host relays this message verbatim onto the
 * WS as ``client_branch_from_checkpoint``; the backend's ``branch_session``
 * call is a graph-state operation that NEVER leaves the local trust
 * boundary. No serialized state or model output is exposed.
 */
import { memo, useCallback, useEffect, useState } from 'react';

export interface MessageActionsProps {
    /** UUID4 hex of the L2-promoted checkpoint to fork from. */
    checkpoint_id: string;
    /** The session that produced this turn (== the LangGraph ``thread_id``). */
    session_id: string;
    /** Index of the assistant message within the session transcript;
     *  forwarded to the host so it can slice the parent's persisted history
     *  at the right point when seeding the new branched session. */
    message_index: number;
    /** True when the source checkpoint carries ``termination_reason ===
     *  "user_abort"`` — flips the icon and the tooltip. */
    is_abort_savepoint?: boolean;
    /** Side-effect callback so the host bridge stays testable: the production
     *  caller passes a function that posts to ``acquireVsCodeApi()``; the
     *  unit test passes a recording stub. */
    post: (msg: {
        type: 'BRANCH_FROM_CHECKPOINT';
        session_id: string;
        checkpoint_id: string;
        message_index: number;
    }) => void;
}

const CONFIRM_REVERT_MS = 3000;

export const MessageActions = memo(function MessageActions(
    props: MessageActionsProps,
): JSX.Element {
    const [confirming, setConfirming] = useState<boolean>(false);

    // Auto-revert the confirm state after 3 s so a stray first click doesn't
    // leave the button stuck in "Confirm?" forever.
    useEffect(() => {
        if (!confirming) { return; }
        const t = setTimeout(() => setConfirming(false), CONFIRM_REVERT_MS);
        return () => clearTimeout(t);
    }, [confirming]);

    const handleClick = useCallback(() => {
        if (!confirming) {
            setConfirming(true);
            return;
        }
        props.post({
            type: 'BRANCH_FROM_CHECKPOINT',
            session_id: props.session_id,
            checkpoint_id: props.checkpoint_id,
            message_index: props.message_index,
        });
        setConfirming(false);
    }, [confirming, props]);

    const isAbort = props.is_abort_savepoint === true;
    const idleIcon = isAbort ? '⏹' : '⟲';
    const idleLabel = isAbort ? 'Rewind from aborted state' : 'Rewind to here';
    const idleTitle = isAbort
        ? 'Rewind to this aborted savepoint and explore a different path — the original conversation stays untouched.'
        : 'Rewind to this checkpoint — forks a new session, the original conversation stays untouched.';

    return (
        <div className="ws-msg-actions" data-abort-savepoint={isAbort}>
            <button
                type="button"
                className="ws-msg-action ws-msg-action-branch"
                data-confirming={confirming}
                onClick={handleClick}
                aria-label={confirming ? `${idleLabel} — click again to confirm` : idleLabel}
                title={confirming
                    ? 'Click again to confirm — the new session will open in the sidebar.'
                    : idleTitle}
            >
                <span aria-hidden="true" className="ws-msg-action-icon">{idleIcon}</span>
                {/* Idle is an icon-only circular control; the confirm step
                    reveals a textual label so the destructive-ish step is
                    never signalled by a bare glyph alone. */}
                <span className="ws-msg-action-label">
                    {confirming ? 'Confirm?' : ''}
                </span>
            </button>
        </div>
    );
});
