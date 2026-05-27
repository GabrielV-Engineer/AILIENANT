/**
 * Phase 7.11.8 (ADR-706 §4.5g) — Per-message action bar (Time-Travel).
 *
 * Rendered under every COMPLETED assistant ``Message`` that carries a
 * ``checkpoint_id`` (i.e. its turn produced an L2-promoted snapshot the user
 * can fork from). Today the only action is "↪ Branch from here"; the
 * component is structured so future actions (Regenerate, Edit + resend)
 * can slot in alongside without disturbing the existing button.
 *
 * UX — two-step confirmation, identical to the 7.11.6 ToolChip retry flow:
 *     first click  → button flips to "↪ Confirm?" with a pulse animation
 *     second click → dispatches BRANCH_FROM_CHECKPOINT to the host
 *     3 s idle     → reverts to idle (no stuck Confirm? state)
 *
 * Abort-savepoint variant — when the source checkpoint was captured by the
 * Phase 7.11.3 emergency-savepoint path (``termination_reason ===
 * "user_abort"``), the icon switches to ⏹ and the tooltip + aria-label make
 * it clear the branch starts from an aborted state. Powerful UX: "go back
 * to before I clicked Stop and try a different path".
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
    const idleIcon = isAbort ? '⏹' : '↪';
    const idleLabel = isAbort ? 'Branch from aborted state' : 'Branch from here';
    const idleTitle = isAbort
        ? 'Fork a new session from this aborted savepoint (Phase 7.11.3) and explore a different path.'
        : 'Fork a new session from this checkpoint — the original conversation stays untouched.';

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
                <span className="ws-msg-action-label">
                    {confirming ? 'Confirm?' : 'Branch'}
                </span>
            </button>
        </div>
    );
});
