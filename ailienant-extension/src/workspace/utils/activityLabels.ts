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
};

/** One-line label for a timeline row, composed from its typed `kind` (+ `target`/`metric`). */
export function timelineEntryLabel(entry: Pick<TimelineEntry, 'kind' | 'target' | 'metric'>): string {
    const verb = KIND_VERB[entry.kind] ?? 'Working';
    switch (entry.kind) {
        case 'read':
        case 'edit':
        case 'heal':
        case 'command':
            return entry.target ? `${verb} ${entry.target}` : verb;
        case 'diff':
            return entry.target
                ? `${verb} ${entry.target}${entry.metric ? ` · ${entry.metric}` : ''}`
                : verb;
        case 'plan':
            return entry.metric ? `Planned ${entry.metric}` : verb;
        default:
            return verb;
    }
}
