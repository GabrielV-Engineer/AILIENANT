/**
 * Glass-Box Timeline — human labels for a typed `TimelineEntry`.
 *
 * Unlike `PipelineProgress`'s predecessor (which pattern-matched raw narrate
 * strings), `TimelineEntry.kind` is already a closed, typed enum — so composing
 * a label here is a direct map, not string-parsing. No raw internal token can
 * ever reach the screen because there is no raw-token code path left to leak.
 */
import type { TimelineEntry, TimelineEntryKind } from '../../shared/config';

const KIND_VERB: Record<TimelineEntryKind, string> = {
    understanding: 'Understanding your request',
    planning: 'Planning',
    reviewing: 'Reviewing the plan',
    read: 'Reading',
    edit: 'Editing',
    command: 'Running',
    retrieval: 'Retrieving context',
    heal: 'Self-healing',
    reasoning: 'Reasoning',
    plan: 'Planned',
    diff: 'Edited',
    cell: 'Agentic cell',
    subagent: 'Dispatched',
};

/**
 * Work-loop phase (13.0.7) a timeline row belongs to — a purely frontend
 * grouping derived from the existing typed `kind` (+ `metric` for a `command`
 * row), with no backend phase axis and no new wire field. `reasoning` and
 * `cell` are excluded (`undefined`): a reasoning span is cross-cutting, not
 * tied to one phase, and a cell iteration is already its own rich, self-
 * contained block — neither belongs grouped under a phase header.
 *
 * `command` needs its `metric` to disambiguate: the marker itself is emitted
 * either by `core/exec_log.py::record_execution` (a real command — the action)
 * or by `_classify_activity`'s "verified "/"giving up on "/"blocked " prefixes
 * (an OUTCOME of verification, not the action) — `metric` is the only signal
 * that survives to the wire distinguishing the two (`'verify'`/`'denied'` vs
 * unset).
 */
export type WorkLoopPhase = 'gather' | 'act' | 'verify';

export function timelineEntryPhase(
    entry: Pick<TimelineEntry, 'kind' | 'metric'>,
): WorkLoopPhase | undefined {
    switch (entry.kind) {
        case 'understanding':
        case 'retrieval':
        case 'read':
            return 'gather';
        case 'planning':
        case 'plan':
        case 'edit':
        case 'diff':
        case 'subagent':
            return 'act';
        case 'reviewing':
        case 'heal':
            return 'verify';
        case 'command':
            return (entry.metric === 'denied' || entry.metric === 'verify') ? 'verify' : 'act';
        default:
            return undefined; // reasoning, cell — not grouped under a phase
    }
}

const PHASE_LABEL: Record<WorkLoopPhase, string> = {
    gather: 'Gathering context',
    act: 'Taking action',
    verify: 'Verifying results',
};

/** Human header for a work-loop phase group. */
export function workLoopPhaseLabel(phase: WorkLoopPhase): string {
    return PHASE_LABEL[phase];
}

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
