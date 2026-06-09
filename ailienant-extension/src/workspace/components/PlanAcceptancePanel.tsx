import React, { useState, useMemo } from 'react';
import { PlanDocumentShape } from '../../shared/config';
import { MarkdownRenderer } from './MarkdownRenderer';

export interface PlanAcceptancePanelProps {
    plan: PlanDocumentShape;
    onAutoAccept: () => void;
    onManualApprove: () => void;
    onKeepPlanning: (feedback: string) => void;
    isStreaming?: boolean;
}

export function PlanAcceptancePanel({
    plan,
    onAutoAccept,
    onManualApprove,
    onKeepPlanning,
    isStreaming = false,
}: PlanAcceptancePanelProps): JSX.Element {
    const [feedback, setFeedback] = useState('');

    // Render plan as markdown (summary + decisions + tasks + checks)
    const planMarkdown = useMemo(() => {
        const lines: string[] = [];

        if (plan.summary) {
            lines.push(`# Plan Summary\n\n${plan.summary}`);
        }

        if (plan.scope && plan.scope.length > 0) {
            lines.push('\n## Scope');
            plan.scope.forEach(s => lines.push(`- ${s}`));
        }

        if (plan.decisions && plan.decisions.length > 0) {
            lines.push('\n## Decisions');
            plan.decisions.forEach(d => lines.push(`- ${d}`));
        }

        if (plan.constraints && plan.constraints.length > 0) {
            lines.push('\n## Constraints');
            plan.constraints.forEach(c => lines.push(`- ${c}`));
        }

        if (plan.tasks && plan.tasks.length > 0) {
            lines.push('\n## Work Breakdown Structure (WBS)');
            plan.tasks.forEach(task => {
                lines.push(`\n### Step ${task.step_number}: ${task.action}`);
                lines.push(`**Role:** ${task.target_role}`);
                lines.push(`**File:** \`${task.target_file}\``);
                lines.push(`**Description:** ${task.description}`);
                lines.push(`**Status:** ${task.status}`);
            });
        }

        if (plan.checks && plan.checks.length > 0) {
            lines.push('\n## Acceptance Checks');
            plan.checks.forEach(check => lines.push(`- [ ] ${check}`));
        }

        return lines.join('\n');
    }, [plan]);

    return (
        <div className="plan-acceptance-panel">
            <div className="plan-acceptance-header">
                <h2>Accept this plan?</h2>
                <p className="subtitle">
                    Select text in the preview to add comments
                </p>
            </div>

            <div className="plan-preview">
                <MarkdownRenderer content={planMarkdown} parserState={undefined} streaming={false} />
            </div>

            <div className="plan-acceptance-buttons">
                <button
                    onClick={onAutoAccept}
                    className="btn-primary"
                    disabled={isStreaming}
                    title="Execute the plan immediately without further review"
                >
                    Yes, and auto-accept
                </button>
                <button
                    onClick={onManualApprove}
                    className="btn-secondary"
                    disabled={isStreaming}
                    title="Execute the plan with approval gate on each edit"
                >
                    Yes, and manually approve edits
                </button>
                <button
                    onClick={() => {
                        onKeepPlanning(feedback);
                        setFeedback('');
                    }}
                    className="btn-tertiary"
                    disabled={isStreaming}
                    title="Refine the plan with a note, or dismiss to keep editing in the composer"
                >
                    No, keep planning
                </button>
            </div>

            <div className="plan-feedback-hud">
                <input
                    type="text"
                    placeholder="Tell AILIENANT what to do instead."
                    value={feedback}
                    onChange={(e) => setFeedback(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && feedback.trim() && !isStreaming) {
                            onKeepPlanning(feedback);
                            setFeedback('');
                        }
                    }}
                    disabled={isStreaming}
                    className="plan-feedback-input"
                />
            </div>
        </div>
    );
}
