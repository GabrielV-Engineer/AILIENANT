/**
 * Glass-Box Timeline loader — phrase pools for the "no concrete detail" kinds
 * (13.1.9).
 *
 * `understanding` / `planning` / `reviewing` have no natural target string
 * (no filename, no tool name, no hit count) — `activityLabels.ts`'s label for
 * them is already generic filler text, so cycling through equivalent
 * phrasings while the loader sits on one of these for a slow step adds
 * variety without inventing anything: the underlying event is still real,
 * only the wording describing it varies. Every OTHER kind (read/edit/tool/
 * retrieval/…) keeps its one exact `timelineEntryLabel` — real data is
 * already more informative than ambient variety, so it is never pooled.
 *
 * Pure and timer-free: the cadence lives in the component that calls
 * `poolPhrase` on an interval, not here.
 */
import type { TimelineEntryKind } from '../../shared/config';

const PHRASE_POOLS: Partial<Record<TimelineEntryKind, readonly string[]>> = {
    understanding: [
        "Understanding your request",
        "Reading through what you're asking",
        'Getting oriented',
        'Weighing what matters here',
    ],
    planning: [
        'Planning',
        'Mapping out the steps',
        'Working out the approach',
        'Sequencing the work',
    ],
    reviewing: [
        'Reviewing the plan',
        'Checking the plan holds together',
        'Double-checking the steps',
    ],
};

/** Whether `kind` cycles through a phrase pool at all. */
export function hasPhrasePool(kind: TimelineEntryKind): boolean {
    return kind in PHRASE_POOLS;
}

/** The phrase for `kind` at pool position `index` (wraps). `undefined` for a
 *  kind with no pool — the caller falls back to `timelineEntryLabel`. */
export function poolPhrase(kind: TimelineEntryKind, index: number): string | undefined {
    const pool = PHRASE_POOLS[kind];
    if (!pool || pool.length === 0) { return undefined; }
    return pool[index % pool.length];
}
