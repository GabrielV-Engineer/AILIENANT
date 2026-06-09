/**
 * Inline per-diff HITL action row (ADR-724).
 *
 * Co-locates the human authorization decision with the diff it concerns. It is
 * rendered ONLY while a `server_hitl_approval_request` is pending; the decision
 * is per-PATCH (one approval_id), not per-hunk — true per-hunk approval is a
 * backend change deferred to a later phase. The buttons drive the SAME
 * approval_id (and the same shared responder) as the Natt-pane card, so the two
 * surfaces resolve as one.
 *
 * Request changes = revise: a decline that carries actionable feedback. The
 * feedback releases the pending approval AND is re-submitted as a fresh turn, so
 * the agent re-proposes against it. The note input is LOCAL component state — it
 * never touches the composer draft (`draftMessages`), so opening it mid-task
 * cannot clobber what the user was typing.
 */
import { useCallback, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import type { HitlRespond } from '../utils/useHitlResponder';

interface Props {
    onRespond: HitlRespond;
    /** Decline + re-submit the note so the agent re-proposes against it. */
    onRequestChanges: (feedback: string) => void;
}

export function DiffHitlActions({ onRespond, onRequestChanges }: Props): JSX.Element {
    const [commenting, setCommenting] = useState(false);
    const [note, setNote] = useState('');
    const inputRef = useRef<HTMLTextAreaElement>(null);

    const accept = useCallback(() => onRespond(true), [onRespond]);
    const reject = useCallback(() => onRespond(false), [onRespond]);

    const openComment = useCallback(() => {
        setCommenting(true);
        // Defer focus to after the textarea mounts.
        window.setTimeout(() => inputRef.current?.focus(), 0);
    }, []);

    // A note is a request to revise (decline + re-propose against the feedback);
    // an empty note degrades to a plain reject so the button never sends a
    // meaningless decision.
    const submitNote = useCallback(() => {
        const trimmed = note.trim();
        if (trimmed) { onRequestChanges(trimmed); }
        else { onRespond(false); }
    }, [note, onRespond, onRequestChanges]);

    const onNoteKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submitNote();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            setCommenting(false);
            setNote('');
        }
    }, [submitNote]);

    return (
        <div className="ws-diff-hitl">
            <div className="ws-diff-hitl-row">
                <Tooltip content="Authorize this change (Ctrl+Enter on the diff)">
                    <button className="ai-btn" data-variant="primary" type="button" onClick={accept} aria-label="Accept change">
                        <Icon name="check" size={13} /><span>Accept</span>
                    </button>
                </Tooltip>
                <Tooltip content="Reject and abort this change (Esc on the diff)">
                    <button className="ai-btn" data-variant="danger" type="button" onClick={reject} aria-label="Reject change">
                        <Icon name="x" size={13} /><span>Reject</span>
                    </button>
                </Tooltip>
                <Tooltip content="Add a note so the agent revises and re-proposes">
                    <button className="ai-btn" type="button" onClick={openComment} aria-label="Request changes" aria-expanded={commenting}>
                        <Icon name="pencil" size={13} /><span>Request changes</span>
                    </button>
                </Tooltip>
            </div>
            {commenting && (
                <div className="ws-diff-hitl-input">
                    <textarea
                        ref={inputRef}
                        className="ai-input"
                        value={note}
                        placeholder="What should change? (Ctrl+Enter to send back, Esc to cancel)"
                        onChange={(e) => setNote(e.target.value)}
                        onKeyDown={onNoteKey}
                    />
                    <button className="ai-btn" data-variant="primary" type="button" onClick={submitNote}>
                        Send back
                    </button>
                </div>
            )}
        </div>
    );
}
