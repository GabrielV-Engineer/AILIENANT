/**
 * Shared dispatch for a Human-In-The-Loop approval response (ADR-724).
 *
 * A single approval (`approval_id`) can be surfaced on more than one webview
 * surface at once — the Natt-pane card AND the inline action row under a diff.
 * Both must drive the SAME decision and post exactly once, so the post + the
 * single-resolve guard live here rather than being duplicated per surface. The
 * first `respond` wins; later calls (from the other surface, or a stray key) are
 * no-ops. The host forwards the message as `client_hitl_response`; `comment` and
 * `modified_content` already exist on the wire contract, so nothing server-side
 * changes.
 */
import { useCallback, useRef } from 'react';
import { vscode } from '../vscode_bridge';

interface RespondOptions {
    comment?: string;
    modified_content?: string;
}

/** The decision dispatcher shared by every HITL surface (card + inline diff row). */
export type HitlRespond = (approved: boolean, opts?: RespondOptions) => void;

export interface HitlResponder {
    respond: HitlRespond;
    /** True once a decision has been posted, so a surface can disable its controls. */
    resolvedRef: React.MutableRefObject<boolean>;
}

export function useHitlResponder(
    approvalId: string,
    onResolved: (approvalId: string) => void,
): HitlResponder {
    const resolvedRef = useRef(false);

    const respond = useCallback((approved: boolean, opts?: RespondOptions) => {
        if (resolvedRef.current) { return; }
        resolvedRef.current = true;
        vscode.postMessage({
            type: 'HITL_RESPONSE',
            approval_id: approvalId,
            approved,
            comment: opts?.comment,
            modified_content: opts?.modified_content,
        });
        onResolved(approvalId);
    }, [approvalId, onResolved]);

    return { respond, resolvedRef };
}
