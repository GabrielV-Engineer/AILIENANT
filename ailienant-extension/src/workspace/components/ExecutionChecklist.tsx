/**
 * Progressive execution checklist.
 *
 * Renders the accepted plan's WBS as a task list whose rows flip ☐ → 🔄 → ✅ / ✗
 * as the backend emits per-step status mutations. Seeded from server_plan_document
 * and updated by server_graph_mutation; it is durable audit evidence of what the
 * agent completed (persisted with the transcript).
 *
 * Pure derived view — memoized on the step count plus the per-step status signature
 * so an unrelated parent re-render (a composer keystroke) never reconciles it.
 */
import { memo } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import type { PlanWBSStep } from '../../shared/config';

interface Props {
    tasks: PlanWBSStep[];
}

function statusGlyph(status: string): { name: IconName; cls: string; spin?: boolean } {
    switch (status) {
        case 'completed': return { name: 'check-circle', cls: 'is-done' };
        case 'failed':    return { name: 'x-circle', cls: 'is-failed' };
        case 'in_progress': return { name: 'loader', cls: 'is-active', spin: true };
        default:          return { name: 'square', cls: 'is-pending' };
    }
}

function ExecutionChecklistImpl({ tasks }: Props): JSX.Element | null {
    if (tasks.length === 0) { return null; }
    const done = tasks.filter(t => t.status === 'completed').length;
    return (
        <div className="ws-checklist" aria-label="Execution checklist">
            <div className="ws-checklist-head">
                Plan · {done}/{tasks.length} done
            </div>
            <ol className="ws-checklist-rows">
                {tasks.map((t) => {
                    const g = statusGlyph(t.status);
                    return (
                        <li key={t.step_number} className={`ws-checklist-row ${g.cls}`} data-status={t.status}>
                            <Icon name={g.name} size={13} className={g.spin ? 'ws-checklist-spin' : undefined} />
                            <span className="ws-checklist-desc">{t.description}</span>
                        </li>
                    );
                })}
            </ol>
        </div>
    );
}

/** Re-render only when a row is added or a status changes. */
export const ExecutionChecklist = memo(ExecutionChecklistImpl, (a, b) => {
    if (a.tasks.length !== b.tasks.length) { return false; }
    for (let i = 0; i < a.tasks.length; i++) {
        if (a.tasks[i].status !== b.tasks[i].status) { return false; }
        if (a.tasks[i].step_number !== b.tasks[i].step_number) { return false; }
    }
    return true;
});
