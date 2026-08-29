/**
 * activityLabels.ts — human labels for a typed TimelineEntry.
 *
 * Pure function tests, no JSDOM/React needed. The 13.0.7 work-loop phase
 * derivation this file used to also cover (`timelineEntryPhase`/
 * `workLoopPhaseLabel`) was deleted in 13.1.9 in favor of agent lanes
 * (`utils/agentLanes.ts`) — see that module's own test file.
 */
import * as assert from 'assert';
import { timelineEntryLabel } from '../workspace/utils/activityLabels';
import type { TimelineEntryKind } from '../shared/config';

function entry(kind: TimelineEntryKind, metric?: string): { kind: TimelineEntryKind; metric?: string } {
    return { kind, metric };
}

suite('13.1.9 — timelineEntryLabel: tool kind', () => {
    test('a tool call reads as itself, not as a shell command', () => {
        assert.strictEqual(
            timelineEntryLabel({ kind: 'tool', target: 'grep_index', metric: '14 hits' }),
            'grep_index · 14 hits',
        );
    });

    test('a tool call with no metric shows just the name', () => {
        assert.strictEqual(timelineEntryLabel({ kind: 'tool', target: 'read_file' }), 'read_file');
    });

    test('a denied tool call is labeled Blocked, like a denied command', () => {
        assert.strictEqual(
            timelineEntryLabel({ kind: 'tool', target: 'run_command', metric: 'denied' }),
            'Blocked run_command',
        );
    });

    test('a tool entry with no target at all falls back to a generic label', () => {
        assert.strictEqual(timelineEntryLabel(entry('tool')), 'Tool call');
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
