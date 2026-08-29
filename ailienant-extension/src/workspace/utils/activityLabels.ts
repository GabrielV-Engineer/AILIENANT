/**
 * Glass-Box Timeline — human labels for a typed `TimelineEntry`.
 *
 * Unlike `PipelineProgress`'s predecessor (which pattern-matched raw narrate
 * strings), `TimelineEntry.kind` is already a closed, typed enum — so composing
 * a label here is a direct map, not string-parsing. No raw internal token can
 * ever reach the screen because there is no raw-token code path left to leak.
 *
 * 13.1.9 — the work-loop phase grouping this module used to derive
 * (`timelineEntryPhase`/`workLoopPhaseLabel`, 13.0.7) was deleted rather than
 * kept alongside agent lanes (`utils/agentLanes.ts`): a turn with two rows
 * rendered as two single-row phase groups, which read as a broken timeline
 * rather than a short one, and a second grouping axis over the same handful
 * of rows was redundant with the lane the agent attribution already implies.
 */
import type { TimelineEntry, TimelineEntryKind } from '../../shared/config';

const KIND_VERB: Record<TimelineEntryKind, string> = {
    understanding: 'Understanding your request',
    planning: 'Planning',
    reviewing: 'Reviewing the plan',
    read: 'Reading',
    edit: 'Editing',
    command: 'Running',
    tool: '',
    retrieval: 'Retrieving context',
    heal: 'Self-healing',
    reasoning: 'Reasoning',
    plan: 'Planned',
    diff: 'Edited',
    cell: 'Agentic cell',
    subagent: 'Dispatched',
};

/** One-line label for a timeline row, composed from its typed `kind` (+ `target`/`metric`). */
export function timelineEntryLabel(entry: Pick<TimelineEntry, 'kind' | 'target' | 'metric'>): string {
    const verb = KIND_VERB[entry.kind] ?? 'Working';
    switch (entry.kind) {
        case 'read':
        case 'edit':
        case 'heal':
            return entry.target ? `${verb} ${entry.target}` : verb;
        case 'command':
            // A command a permission gate or the dangerous-pattern intercept
            // refused never reaches an adapter — labeling it "Running" would
            // claim the opposite of what happened.
            if (entry.metric === 'denied') {
                return entry.target ? `Blocked ${entry.target}` : 'Blocked';
            }
            return entry.target ? `${verb} ${entry.target}` : verb;
        case 'tool':
            // A tool call reads as itself — "grep_index · 14 hits" — not as a
            // shell command (13.1.9's fix for the two reading identically).
            if (entry.metric === 'denied') {
                return entry.target ? `Blocked ${entry.target}` : 'Blocked';
            }
            if (!entry.target) { return 'Tool call'; }
            return entry.metric ? `${entry.target} · ${entry.metric}` : entry.target;
        case 'diff':
            return entry.target
                ? `${verb} ${entry.target}${entry.metric ? ` · ${entry.metric}` : ''}`
                : verb;
        case 'retrieval':
            // target is the file the lookup was scoped to (coder.py); metric is
            // the hit count (coder.py and researcher.py both supply it, the
            // latter with no target — a workspace-wide lookup, not one file).
            return entry.target
                ? `${verb}: ${entry.target}${entry.metric ? ` · ${entry.metric}` : ''}`
                : (entry.metric ? `${verb} · ${entry.metric}` : verb);
        case 'subagent':
            // target is the dispatched role, metric its outcome status.
            return entry.target
                ? `${verb} ${entry.target}${entry.metric ? ` · ${entry.metric}` : ''}`
                : verb;
        case 'plan':
            return entry.metric ? `Planned ${entry.metric}` : verb;
        case 'cell':
            return entry.metric ? `${verb} · ${entry.metric}` : verb;
        default:
            return verb;
    }
}
