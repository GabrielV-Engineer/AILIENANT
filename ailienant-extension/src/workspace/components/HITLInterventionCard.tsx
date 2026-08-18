import { useState, useEffect, useCallback, useRef } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { useHitlResponder } from '../utils/useHitlResponder';
import type { ClarificationQuestion } from '../../api/contracts';

export interface HITLIntervention {
    approval_id: string;
    action_description: string;
    proposed_content?: string;
    /** Classifier string forwarded from the backend payload.  Known values:
     *  FILE_WRITE, BUDGET_OVERFLOW, SANDBOX_DEGRADED_EXEC, MCP_TOOL_CALL,
     *  RISK_INTERCEPT (YOLO Guard — risky command in a permissive session).
     *  Unknown / absent values degrade gracefully to the generic card layout. */
    request_kind?: string | null;
    /** Pattern category labels matched against the proposed content — a risky
     *  shell command (RISK_INTERCEPT) or an edit's added diff lines (FILE_WRITE).
     *  Rendered whenever present, regardless of request_kind, so an auto-accept
     *  edit that got routed to manual review can say why. */
    risk_patterns_matched?: string[] | null;
    /** DEBT-172: present on a CLARIFICATION_NEEDED / ASK_USER_QUESTION card.
     *  `question`/`context`/`suggested_options` back a single question;
     *  `questions` is the multi-question batch extension. When either is
     *  present, Workspace.tsx renders ClarificationGrillCard instead of this
     *  approve/reject card. */
    question?: string | null;
    context?: string | null;
    suggested_options?: string[] | null;
    questions?: ClarificationQuestion[] | null;
}

interface Props {
    intervention: HITLIntervention;
    nattName: string;
    onResolved: (approvalId: string) => void;
}

export function HITLInterventionCard({ intervention, nattName, onResolved }: Props): JSX.Element {
    const [editMode, setEditMode] = useState(false);
    const [editedContent, setEditedContent] = useState(intervention.proposed_content ?? '');
    const cardRef = useRef<HTMLDivElement>(null);
    // The post + single-resolve guard (the in-chat card, the native OS toast and
    // the inline per-diff row can all target one approval_id) live in the shared
    // responder so the surfaces cannot diverge — only the first call posts.
    const { respond: respondRaw } = useHitlResponder(intervention.approval_id, onResolved);
    const respond = useCallback((approved: boolean, modified?: string) => {
        respondRaw(approved, modified !== undefined ? { modified_content: modified } : undefined);
    }, [respondRaw]);

    useEffect(() => {
        const onKey = (e: KeyboardEvent): void => {
            if (e.key === 'Escape') { e.preventDefault(); respond(false); }
            else if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); respond(true, editMode ? editedContent : undefined); }
            else if (e.key === 'F2') { e.preventDefault(); setEditMode(m => !m); }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [respond, editMode, editedContent]);

    const isMcpCall = intervention.request_kind === 'MCP_TOOL_CALL';
    const isRiskIntercept = intervention.request_kind === 'RISK_INTERCEPT';

    return (
        <div ref={cardRef} className="ws-hitl-card ai-card" role="alertdialog" aria-live="assertive">
            <div className="ws-hitl-head">
                <Icon
                    name={isMcpCall ? 'plug' : isRiskIntercept ? 'alert' : 'key'}
                    size={16}
                    color={isRiskIntercept ? 'var(--accent-error)' : 'var(--accent-warn)'}
                />
                <span className="ws-hitl-title">
                    {isMcpCall
                        ? `MCP tool call — ${nattName} requires your authorization`
                        : isRiskIntercept
                            ? `Auto mode intercepted — risky pattern detected`
                            : `${nattName} requires your authorization`}
                </span>
            </div>
            {isMcpCall && (
                <div className="ws-hitl-section" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    Approving once trusts this tool for the remainder of the current task session.
                </div>
            )}
            {intervention.risk_patterns_matched && intervention.risk_patterns_matched.length > 0 && (
                <div className="ws-hitl-section" style={{ fontSize: 11, color: 'var(--accent-error)' }}>
                    {isRiskIntercept ? 'Detected: ' : 'Flagged for: '}
                    {intervention.risk_patterns_matched.join(', ')}
                </div>
            )}

            <div className="ws-hitl-section">
                <div className="ws-hitl-label">Action proposed</div>
                <div className="ws-hitl-action">{intervention.action_description}</div>
            </div>

            {intervention.proposed_content && (
                <div className="ws-hitl-section">
                    <div className="ws-hitl-label">Proposed payload {editMode && <em>(editing)</em>}</div>
                    {editMode ? (
                        <textarea
                            className="ai-input"
                            style={{ minHeight: 120, fontFamily: 'monospace', fontSize: 12 }}
                            value={editedContent}
                            onChange={(e) => setEditedContent(e.target.value)}
                        />
                    ) : (
                        <pre className="ws-hitl-payload">{intervention.proposed_content.slice(0, 800)}{intervention.proposed_content.length > 800 ? '…' : ''}</pre>
                    )}
                </div>
            )}

            <div className="ws-hitl-actions">
                <Tooltip content="Reject and abort this action (Esc)">
                    <button className="ai-btn" data-variant="danger" onClick={() => respond(false)} aria-label="Cancel / Deny">
                        <Icon name="x" size={14} /><span>Cancel / Deny</span>
                    </button>
                </Tooltip>
                {intervention.proposed_content && (
                    <Tooltip content="Edit the proposed payload before authorizing (F2)">
                        <button className="ai-btn" onClick={() => setEditMode(m => !m)} aria-label="Modify parameters">
                            <Icon name="pencil" size={14} /><span>Modify Parameters</span>
                        </button>
                    </Tooltip>
                )}
                <Tooltip content="Authorize execution as proposed (Ctrl+Enter)">
                    <button
                        className="ai-btn"
                        data-variant="primary"
                        onClick={() => respond(true, editMode ? editedContent : undefined)}
                        aria-label="Authorize execution"
                    >
                        <Icon name="check" size={14} /><span>Authorize Execution</span>
                    </button>
                </Tooltip>
            </div>
        </div>
    );
}
