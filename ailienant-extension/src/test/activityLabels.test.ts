/**
 * activityLabels.ts — work-loop phase derivation (13.0.7).
 *
 * Pure function tests, no JSDOM/React needed.
 */
import * as assert from 'assert';
import { timelineEntryLabel, timelineEntryPhase, workLoopPhaseLabel } from '../workspace/utils/activityLabels';
import type { TimelineEntryKind } from '../shared/config';

function entry(kind: TimelineEntryKind, metric?: string): { kind: TimelineEntryKind; metric?: string } {
    return { kind, metric };
}

suite('13.0.7 — timelineEntryPhase', () => {
    test('gather kinds: understanding, retrieval, read', () => {
        assert.strictEqual(timelineEntryPhase(entry('understanding')), 'gather');
        assert.strictEqual(timelineEntryPhase(entry('retrieval')), 'gather');
        assert.strictEqual(timelineEntryPhase(entry('read')), 'gather');
    });

    test('act kinds: planning, plan, edit, diff, subagent', () => {
        assert.strictEqual(timelineEntryPhase(entry('planning')), 'act');
        assert.strictEqual(timelineEntryPhase(entry('plan')), 'act');
        assert.strictEqual(timelineEntryPhase(entry('edit')), 'act');
        assert.strictEqual(timelineEntryPhase(entry('diff')), 'act');
        assert.strictEqual(timelineEntryPhase(entry('subagent')), 'act');
    });

    test('verify kinds: reviewing, heal', () => {
        assert.strictEqual(timelineEntryPhase(entry('reviewing')), 'verify');
        assert.strictEqual(timelineEntryPhase(entry('heal')), 'verify');
    });

    test('reasoning and cell are excluded — no phase, never grouped under a header', () => {
        assert.strictEqual(timelineEntryPhase(entry('reasoning')), undefined);
        assert.strictEqual(timelineEntryPhase(entry('cell')), undefined);
    });

    test('command disambiguates via metric: a real exec (no metric) is act', () => {
        assert.strictEqual(timelineEntryPhase(entry('command')), 'act');
        assert.strictEqual(timelineEntryPhase(entry('command', 'some-other-metric')), 'act');
    });

    test('command with metric=denied or metric=verify is a verification outcome, not the action', () => {
        assert.strictEqual(timelineEntryPhase(entry('command', 'denied')), 'verify');
        assert.strictEqual(timelineEntryPhase(entry('command', 'verify')), 'verify');
    });

    test('workLoopPhaseLabel gives a human header for each phase', () => {
        assert.strictEqual(workLoopPhaseLabel('gather'), 'Gathering context');
        assert.strictEqual(workLoopPhaseLabel('act'), 'Taking action');
        assert.strictEqual(workLoopPhaseLabel('verify'), 'Verifying results');
    });
});

suite('13.0.9 — timelineEntryLabel: retrieval and subagent kinds', () => {
    // Regression guard: 'retrieval' used to fall through to the generic
    // `default: return verb` branch — target/metric were silently ignored, so
    // wiring the backend to actually emit them (coder.py/researcher.py) would
    // have had no visible effect at all.
    test('retrieval with a target (coder.py: scoped to one file) shows the file and hit count', () => {
        assert.strictEqual(
            timelineEntryLabel({ kind: 'retrieval', target: 'calc.py', metric: '3 snippet(s)' }),
            'Retrieving context: calc.py · 3 snippet(s)',
        );
    });

    test('retrieval with no target (researcher.py: workspace-wide) shows just the count', () => {
        assert.strictEqual(
            timelineEntryLabel({ kind: 'retrieval', metric: '2 file(s)' }),
            'Retrieving context · 2 file(s)',
        );
    });

    test('retrieval with neither target nor metric falls back to the bare verb', () => {
        assert.strictEqual(timelineEntryLabel({ kind: 'retrieval' }), 'Retrieving context');
    });

    test('subagent shows the dispatched role and its outcome', () => {
        assert.strictEqual(
            timelineEntryLabel({ kind: 'subagent', target: 'core_dev', metric: 'ok' }),
            'Dispatched core_dev · ok',
        );
    });

    test('subagent with no target falls back to the bare verb', () => {
        assert.strictEqual(timelineEntryLabel({ kind: 'subagent' }), 'Dispatched');
    });
});
