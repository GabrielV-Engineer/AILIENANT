/**
 * Shared dispatch for a multi-question clarification response (DEBT-172).
 *
 * Mirrors `useHitlResponder`'s single-post guard: the card's own "Submit
 * answers" button is the only trigger (there is no second surface racing to
 * resolve a clarification the way an inline diff row races the in-chat HITL
 * card), but the guard is kept anyway so a stray double-click can't post
 * twice. The host forwards the message as `client_hitl_response`; `answers`
 * already exists on the wire contract (`HITLResponsePayload.answers`), so
 * nothing server-side changes.
 */
import { useCallback, useRef } from 'react';
import { vscode } from '../vscode_bridge';
import { useChatStore } from '../chatStore';
import type { ClarificationAnswer } from '../../api/contracts';

export interface ClarificationResponder {
    respond: (answers: ClarificationAnswer[]) => void;
    resolvedRef: React.MutableRefObject<boolean>;
}

export function useClarificationResponder(
    approvalId: string,
    onResolved: (approvalId: string) => void,
): ClarificationResponder {
    const resolvedRef = useRef(false);

    const respond = useCallback((answers: ClarificationAnswer[]) => {
        if (resolvedRef.current) { return; }
        resolvedRef.current = true;
        // Submitting answers resumes the paused graph, which may run another
        // grounding + question-generation round with no tokens for a while —
        // re-arm the working indicator so the Stop button stays live and the
        // turn never appears to go idle.
        useChatStore.getState().setIsTurnActive(true);
        vscode.postMessage({
            type: 'HITL_RESPONSE',
            approval_id: approvalId,
            approved: true,
            answers,
        });
        onResolved(approvalId);
    }, [approvalId, onResolved]);

    return { respond, resolvedRef };
}
