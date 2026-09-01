/**
 * Brief-review decision logic — pure, no React and no vscode bridge.
 *
 * Extracted from `BriefReviewCard` for the same reason `clarificationLogic.ts`
 * was extracted from its card: this project's mocha suite tests component logic
 * directly rather than rendering into a DOM, and importing anything that reaches
 * `vscode_bridge.ts` triggers its eager `acquireVsCodeApi()` call, which throws
 * outside a real WebView.
 *
 * What is worth pinning here is which resume payload each action produces — the
 * brief becomes the planner's literal input, so "accepted unchanged" and
 * "accepted with an edit" must not be confusable.
 */

export type BriefAction = 'accept' | 'rewrite' | 'cancel';

export interface BriefDecision {
    approved: boolean;
    /** Present only for a rewrite carrying an actual instruction. */
    comment?: string;
    /** Present only when the operator genuinely changed the text. */
    modified_content?: string;
}

/**
 * Whether Send back may fire. A rewrite carries its steer in `comment`, and the
 * backend re-distils only when one is present — so a blank note produced a
 * payload identical to a cancel, ending the turn and re-rendering the very same
 * brief. That reads as "I asked for changes and got the same text back". The
 * action is refused instead, exactly as `canAcceptBrief` refuses an empty brief.
 */
export function canSendBriefBack(note: string): boolean {
    return note.trim().length > 0;
}

/**
 * Whether Accept may fire. An emptied brief cannot be accepted: the backend
 * treats a blank `modified_content` as absent and would silently hand off the
 * original — so the operator would have deleted everything and got the draft
 * back anyway. Refusing the action is honest; silently ignoring it is not.
 */
export function canAcceptBrief(draft: string): boolean {
    return draft.trim().length > 0;
}

/** Map an operator action plus the current editor state onto a resume payload. */
export function buildBriefDecision(
    action: BriefAction,
    original: string,
    draft: string,
    note: string,
): BriefDecision {
    if (action === 'accept') {
        // Exact comparison: any change the operator made is theirs to keep. An
        // untouched draft sends NO modified_content, so the planner receives the
        // backend's own text rather than a round-tripped copy of it.
        return draft === original ? { approved: true } : { approved: true, modified_content: draft };
    }
    if (action === 'rewrite') {
        const trimmed = note.trim();
        // A rewrite with nothing to steer by IS a cancel on the wire — the
        // backend cannot tell them apart. `canSendBriefBack` gates the button so
        // this branch is unreachable from the UI; it stays as the fail-safe for
        // any other caller.
        return trimmed ? { approved: false, comment: trimmed } : { approved: false };
    }
    return { approved: false };
}
