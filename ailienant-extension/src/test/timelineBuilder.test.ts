/**
 * Glass-Box Timeline (11.5.C.1) — pure builder contract tests.
 *
 * Covers the order-agnostic correlation guarantee: a body event (reasoning delta,
 * diff) may arrive before OR after its `server_activity_event` marker, and either
 * arrival order must converge on the same final entry, correctly `seq`-ordered.
 */
import * as assert from 'assert';
import type { ActivityEventPayload } from '../api/contracts';
import type { DiffBlockShape, TimelineEntry } from '../shared/config';
import {
    upsertActivityMarker, upsertReasoningDelta, upsertDiffBody, upsertToolBody,
} from '../workspace/utils/timelineBuilder';

function marker(over: Partial<ActivityEventPayload> & { seq: number; kind: ActivityEventPayload['kind'] }): ActivityEventPayload {
    return { session_id: 's1', ts: 100 + over.seq, ...over };
}

suite('11.5.C.1 — timelineBuilder', () => {

    // ── Self-contained markers (read/edit/command/understanding/…) ──────────

    test('a ref-less marker creates a standalone, immediately-settled entry', () => {
        const entries = upsertActivityMarker([], marker({ seq: 0, kind: 'understanding' }));
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'seq:0');
        assert.strictEqual(entries[0].status, 'done');
        assert.strictEqual(entries[0].kind, 'understanding');
    });

    test('markers accumulate in seq order regardless of kind', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({ seq: 0, kind: 'understanding' }));
        entries = upsertActivityMarker(entries, marker({ seq: 1, kind: 'read', target: 'a.py' }));
        entries = upsertActivityMarker(entries, marker({ seq: 2, kind: 'edit', target: 'a.py' }));
        assert.deepStrictEqual(entries.map(e => e.seq), [0, 1, 2]);
        assert.deepStrictEqual(entries.map(e => e.kind), ['understanding', 'read', 'edit']);
    });

    // ── Reasoning — order-agnostic correlation by ref ────────────────────────

    test('reasoning: marker arrives FIRST, deltas attach by ref', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({ seq: 3, kind: 'reasoning', ref: 'r1' }));
        entries = upsertReasoningDelta(entries, 'Let me think ', 'r1', 100);
        entries = upsertReasoningDelta(entries, 'about this.', 'r1', 100);

        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'r1');
        assert.strictEqual(entries[0].seq, 3);
        assert.strictEqual(entries[0].status, 'active');
        assert.strictEqual(entries[0].thinking, 'Let me think about this.');
    });

    test('reasoning: deltas arrive FIRST (marker throttled/delayed), still converge', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertReasoningDelta(entries, 'Let me think ', 'r1', 100);
        entries = upsertReasoningDelta(entries, 'about this.', 'r1', 100);
        // Placeholder, unresolved seq — sorts last until the marker arrives.
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, Number.POSITIVE_INFINITY);
        assert.strictEqual(entries[0].thinking, 'Let me think about this.');

        entries = upsertActivityMarker(entries, marker({ seq: 3, kind: 'reasoning', ref: 'r1' }));
        // Adopted: real seq now set, thinking body preserved, no duplicate entry.
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'r1');
        assert.strictEqual(entries[0].seq, 3);
        assert.strictEqual(entries[0].thinking, 'Let me think about this.');
    });

    test('two interleaved markers sandwiching out-of-order reasoning still sort correctly', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({ seq: 0, kind: 'understanding' }));
        entries = upsertReasoningDelta(entries, 'thinking...', 'r1', 100); // body-first, unresolved
        entries = upsertActivityMarker(entries, marker({ seq: 2, kind: 'read', target: 'a.py' }));
        entries = upsertActivityMarker(entries, marker({ seq: 1, kind: 'reasoning', ref: 'r1' })); // resolves r1 to seq 1

        assert.deepStrictEqual(entries.map(e => e.seq), [0, 1, 2]);
        assert.deepStrictEqual(entries.map(e => e.id), ['seq:0', 'r1', 'seq:2']);
        assert.strictEqual(entries[1].thinking, 'thinking...');
    });

    // ── Diff — order-agnostic correlation by file_path ───────────────────────

    function diffBlock(path: string): DiffBlockShape {
        return {
            patch_id: 'p1', file_path: path, old_content: 'a', new_content: 'b', status: 'edit',
        };
    }

    test('diff: marker arrives first, body attaches by file_path ref', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({
            seq: 5, kind: 'diff', target: 'calc.py', ref: 'calc.py', metric: '+2 -2',
        }));
        entries = upsertDiffBody(entries, 'calc.py', diffBlock('calc.py'), 100);

        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'calc.py');
        assert.strictEqual(entries[0].seq, 5);
        assert.strictEqual(entries[0].metric, '+2 -2');
        assert.ok(entries[0].diff);
        assert.strictEqual(entries[0].diff!.file_path, 'calc.py');
    });

    test('diff: body arrives first, marker resolves it (no duplicate)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertDiffBody(entries, 'calc.py', diffBlock('calc.py'), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, Number.POSITIVE_INFINITY);

        entries = upsertActivityMarker(entries, marker({
            seq: 5, kind: 'diff', target: 'calc.py', ref: 'calc.py', metric: '+2 -2',
        }));
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, 5);
        assert.ok(entries[0].diff, 'diff body must survive the marker merge');
    });

    test('two different files never collide (distinct refs)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertDiffBody(entries, 'a.py', diffBlock('a.py'), 100);
        entries = upsertDiffBody(entries, 'b.py', diffBlock('b.py'), 100);
        assert.strictEqual(entries.length, 2);
        assert.deepStrictEqual(entries.map(e => e.id).sort(), ['a.py', 'b.py']);
    });

    // ── Tool — same identity-keyed mechanism (forward-use, unwired this slice) ──

    test('tool body upserts and updates status by wire status', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertToolBody(entries, 'call-1', {
            tool_call_id: 'call-1', tool_name: 'pytest', args: {}, status: 'pending', output_lines: [],
        }, 100);
        assert.strictEqual(entries[0].status, 'active');

        entries = upsertToolBody(entries, 'call-1', {
            tool_call_id: 'call-1', tool_name: 'pytest', args: {}, status: 'success',
            output_lines: ['2 passed'], exit_code: 0,
        }, 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].status, 'done');
        assert.strictEqual(entries[0].tool?.exit_code, 0);
    });

    // ── Pure-on-inputs guarantee ──────────────────────────────────────────────

    test('every upsert returns a NEW array — never mutates the input', () => {
        const before: TimelineEntry[] = [];
        const after = upsertActivityMarker(before, marker({ seq: 0, kind: 'understanding' }));
        assert.notStrictEqual(before, after);
        assert.strictEqual(before.length, 0);
    });
});
