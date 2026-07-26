import { useState, useEffect } from 'react';
import { Icon } from '../../shared/Icon';

interface Props {
    steps: string[];
    done?: boolean;
    /** True when the turn carries an ExecutionChecklist — the authoritative per-step
     *  record. When done, this widget self-hides to defer to it (no double surface). */
    hasChecklist?: boolean;
}

/* Internal node tokens the backend narrates are not user-facing. Translate the
   pure-jargon phases to plain phrases; the coder/healer already emit human free-text
   ("reading x.py", "running …", "self-healing …") which passes through capitalized. An
   unrecognized bare token degrades to a generic "Working" rather than leaking on screen. */
const PHASE_LABELS: Record<string, string> = {
    context_gather: 'Understanding your request',
    synthesizing_intent: 'Interpreting your intent',
    handoff_to_planner: 'Planning',
    drafting_spec: 'Planning',
    critic_review: 'Reviewing the plan',
    plan_validated: 'Reviewing the plan',
    unwrapping_schema: 'Reviewing the plan',
    plan_budget_overage_advisory: 'Reviewing the plan',
};
const HUMAN_PREFIXES = ['reading', 'running', 'verified', 'giving up', 'self-healing', 'recovered', 'could not auto-fix'];

function friendlyPhase(raw: string): string {
    const s = (raw ?? '').trim();
    if (!s) { return 'Working'; }
    if (PHASE_LABELS[s]) { return PHASE_LABELS[s]; }
    if (s.startsWith('critic_rejected')) { return 'Revising the plan'; }
    const lower = s.toLowerCase();
    if (HUMAN_PREFIXES.some(p => lower.startsWith(p))) {
        return s.charAt(0).toUpperCase() + s.slice(1);
    }
    // A bare internal token (snake_case, no spaces) we don't recognize → generic label.
    if (/^[a-z0-9_]+$/.test(s)) { return 'Working'; }
    return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Collapsible agent-activity status line.
 *
 * Renders inline as a quiet, borderless status line in the conversation flow
 * (not a floating widget). Collapsed by default: a spinner + the current activity
 * in plain language. Click to expand the full vertical stepper. On completion it
 * defers to the ExecutionChecklist when one exists (self-hides), else collapses to
 * a quiet "Task completed" — never a step count, which would double-count against
 * the checklist. Rendered per assistant turn; nothing when no steps have arrived.
 */
export function PipelineProgress({ steps, done = false, hasChecklist = false }: Props): JSX.Element | null {
    const [expanded, setExpanded] = useState(false);
    useEffect(() => { if (done) { setExpanded(false); } }, [done]);   // auto-collapse on completion
    if (steps.length === 0) {
        return null;
    }
    // On completion the checklist is the canonical per-step record; don't compete with it.
    if (done && hasChecklist) {
        return null;
    }

    const latest = friendlyPhase(steps[steps.length - 1]);
    const label = done ? 'Task completed' : `${latest}…`;

    return (
        <div className={`ws-thinking${done ? ' is-done' : ''}`}>
            <button
                type="button"
                className="ws-thinking-header"
                aria-expanded={expanded}
                onClick={() => setExpanded(e => !e)}
            >
                {done
                    ? <Icon name="check" size={12} className="ws-thinking-check" />
                    : <Icon name="loader" size={12} className="ws-thinking-spin" />}
                <span className="ws-thinking-label">{label}</span>
                <Icon
                    name={expanded ? 'chevron-down' : 'chevron-right'}
                    size={12}
                    className="ws-thinking-chevron"
                />
            </button>
            {expanded && (
                <ol className="ws-thinking-steps">
                    {steps.map((s, i) => (
                        <li
                            key={i}
                            className="ws-thinking-step"
                            data-current={!done && i === steps.length - 1 ? 'true' : 'false'}
                        >
                            <span className="ws-thinking-step-dot" />
                            <span className="ws-thinking-step-name">{friendlyPhase(s)}</span>
                        </li>
                    ))}
                </ol>
            )}
        </div>
    );
}
