import { useState } from 'react';
import { vscode } from '../vscode_bridge';

export interface HITLRequest {
    approval_id: string;
    action_description: string;
    proposed_content?: string;
}

interface Props {
    request: HITLRequest;
    onResolved: (approvalId: string) => void;
}

export function HITLCard({ request, onResolved }: Props): JSX.Element {
    const [comment, setComment] = useState('');

    const respond = (approved: boolean): void => {
        vscode.postMessage({
            type: 'togglePlannerMode',
            value: false,
        });
        // Send HITL response via extension host message channel
        vscode.postMessage({
            type: 'HITL_RESPONSE',
            approval_id: request.approval_id,
            approved,
            comment: comment.trim() || undefined,
        });
        onResolved(request.approval_id);
    };

    return (
        <div className="ai-hitl-card">
            <div className="ai-hitl-card-title">🔑 Human Approval Required</div>
            <div className="ai-hitl-card-desc">{request.action_description}</div>
            {request.proposed_content && (
                <pre style={{
                    fontSize: 10,
                    background: 'rgba(0,0,0,0.15)',
                    padding: '4px 6px',
                    borderRadius: 3,
                    overflow: 'auto',
                    maxHeight: 80,
                    marginBottom: 6,
                }}>
                    {request.proposed_content.slice(0, 400)}
                    {request.proposed_content.length > 400 ? '…' : ''}
                </pre>
            )}
            <textarea
                className="ai-hitl-comment"
                placeholder="Optional comment…"
                value={comment}
                onChange={e => setComment(e.target.value)}
                rows={2}
            />
            <div className="ai-hitl-actions">
                <button className="ai-btn ai-btn-primary" onClick={() => respond(true)}>
                    ✓ Approve
                </button>
                <button className="ai-btn ai-btn-danger" onClick={() => respond(false)}>
                    ✗ Reject
                </button>
            </div>
        </div>
    );
}
