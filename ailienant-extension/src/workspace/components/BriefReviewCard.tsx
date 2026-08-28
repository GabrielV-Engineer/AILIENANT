/**
 * Brief Review — the last, visible step of the Socratic grill.
 *
 * The distillation that closes an interview REPLACES the operator's prompt with a
 * compressed brief, and everything downstream plans against that text. It is the
 * one stage nothing else checks: the planner's critic validates the resulting plan
 * against a schema, never against the dialogue, so a constraint dropped here
 * surfaces as an absence — and no view can render what isn't there. This card is
 * where that absence becomes visible.
 *
 * Three rules follow from that, and none of them is cosmetic:
 *
 *  - **Verbatim.** Rendered as plain monospace text, never markdown, never
 *    truncated. `HITLInterventionCard` slices `proposed_content` at 800 chars,
 *    which would hide part of the very thing under review. What is read here must
 *    be exactly what the planner receives.
 *  - **Its own structure is already in the text.** The backend composes the brief
 *    with the settled constraints and scope as labelled blocks, so reading it
 *    verbatim already shows them as lists. Re-deriving them into separate UI
 *    sections would risk displaying something the planner does not actually get.
 *  - **Editable in place.** It is a prompt; an edit is authoritative and rides the
 *    existing `modified_content` field end to end.
 *
 * Rides the same approval channel and single-resolve guard as every other HITL
 * surface (`useHitlResponder`), so nothing server-side is new.
 */
import { useCallback, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { useHitlResponder } from '../utils/useHitlResponder';
import { buildBriefDecision, canAcceptBrief, type BriefAction } from '../utils/briefReviewLogic';
import type { HITLIntervention } from './HITLInterventionCard';

export const BRIEF_REVIEW_KIND = 'BRIEF_REVIEW';

interface Props {
    intervention: HITLIntervention;
    onResolved: (approvalId: string) => void;
}

export function BriefReviewCard({ intervention, onResolved }: Props): JSX.Element {
    const original = intervention.proposed_content ?? '';
    const [draft, setDraft] = useState(original);
    const [note, setNote] = useState('');
    const [noteOpen, setNoteOpen] = useState(false);
    const noteRef = useRef<HTMLTextAreaElement>(null);
    const { respond, resolvedRef } = useHitlResponder(intervention.approval_id, onResolved);

    const edited = draft !== original;
    const acceptable = canAcceptBrief(draft);

    // Every action routes through the same pure mapping (briefReviewLogic) so the
    // payload semantics are pinned by tests rather than by three call sites here.
    const decide = useCallback((action: BriefAction) => {
        const d = buildBriefDecision(action, original, draft, note);
        respond(d.approved, { comment: d.comment, modified_content: d.modified_content });
    }, [respond, original, draft, note]);

    const accept = useCallback(() => decide('accept'), [decide]);

    const openNote = useCallback(() => {
        setNoteOpen(true);
        window.setTimeout(() => noteRef.current?.focus(), 0);
    }, []);

    const sendBack = useCallback(() => decide('rewrite'), [decide]);
    const cancel = useCallback(() => decide('cancel'), [decide]);

    const onNoteKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            sendBack();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            setNoteOpen(false);
            setNote('');
        }
    }, [sendBack]);

    return (
        <div className="ws-brief-review ai-card" role="group" aria-label="Review the distilled brief">
            <div className="ws-brief-review-head">
                <Icon name="check-circle" size={14} />
                <span className="ws-brief-review-title">Ready to plan — review the brief</span>
            </div>
            <p className="ws-brief-review-sub">
                This is exactly what the planner will work from. Edit it directly, or send
                it back with a correction.
            </p>

            <textarea
                className="ai-input ws-brief-review-body"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                aria-label="Distilled brief"
                disabled={resolvedRef.current}
            />

            <div className="ws-brief-review-actions">
                <Tooltip content={acceptable ? 'Plan from this brief' : 'The brief cannot be empty'}>
                    <button
                        className="ai-btn"
                        data-variant="primary"
                        type="button"
                        onClick={accept}
                        disabled={!acceptable}
                    >
                        <Icon name="check" size={13} />
                        <span>{edited ? 'Accept edits & plan' : 'Accept & plan'}</span>
                    </button>
                </Tooltip>
                <Tooltip content="Send it back to be rewritten from the same dialogue">
                    <button className="ai-btn" type="button" onClick={openNote} aria-expanded={noteOpen}>
                        <Icon name="pencil" size={13} /><span>Rewrite with a note</span>
                    </button>
                </Tooltip>
                <Tooltip content="End this turn — the dialogue is kept, so you can continue it">
                    <button className="ai-btn" data-variant="danger" type="button" onClick={cancel}>
                        <Icon name="x" size={13} /><span>Cancel</span>
                    </button>
                </Tooltip>
            </div>

            {noteOpen && (
                <div className="ws-brief-review-note">
                    <textarea
                        ref={noteRef}
                        className="ai-input"
                        value={note}
                        placeholder="What did it get wrong or leave out? (Ctrl+Enter to send back, Esc to cancel)"
                        onChange={(e) => setNote(e.target.value)}
                        onKeyDown={onNoteKey}
                    />
                    <button className="ai-btn" data-variant="primary" type="button" onClick={sendBack}>
                        Send back
                    </button>
                </div>
            )}
        </div>
    );
}
