/**
 * Glass-Box Timeline (11.5.C.1) — pure builder contract tests.
 *
 * Covers the order-agnostic correlation guarantee: a body event (reasoning delta,
 * diff) may arrive before OR after its `server_activity_event` marker, and either
 * arrival order must converge on the same final entry, correctly `seq`-ordered.
 */
import * as assert from 'assert';
import type { ActivityEventPayload } from '../api/contracts';
import type { CellIterationShape, DiffBlockShape, ExecutionDetailShape, TimelineEntry } from '../shared/config';
import {
    upsertActivityMarker, upsertReasoningDelta, freezeActiveReasoningEntries, upsertDiffBody, upsertCellBody,
    upsertExecutionBody, upsertExecutionChunk, stripReasoningForPersist,
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

    // ── Reasoning — per-entry chronometry, several spans in one turn ────────

    test('a new reasoning span stamps its own tokenCount/startedAt, independent of a prior span', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertReasoningDelta(entries, 'Grounding…', 'r1', 100, { tokenCount: 3, now: 1_000 });
        entries = upsertReasoningDelta(entries, ' more', 'r1', 100, { tokenCount: 5, now: 1_050 });
        assert.strictEqual(entries[0].thinkingTokens, 5, 'tokenCount tracks the latest value, like the message-scoped reducer');
        assert.strictEqual(entries[0].thinkingStartedAt, 1_000, 'startedAt stamps once, on the first delta, and never moves');
        assert.strictEqual(entries[0].thinkingElapsedMs, undefined, 'still open — no freeze yet');
    });

    test('a second reasoning span beginning freezes the first — independent clocks, not a shared one', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertReasoningDelta(entries, 'Grounding…', 'r1', 100, { tokenCount: 3, now: 1_000 });
        entries = upsertReasoningDelta(entries, 'Composing…', 'r2', 100, { tokenCount: 2, now: 1_800 });

        assert.strictEqual(entries.length, 2);
        const r1 = entries.find(e => e.id === 'r1')!;
        const r2 = entries.find(e => e.id === 'r2')!;
        assert.strictEqual(r1.thinkingElapsedMs, 800, 'r1 froze the instant r2 started (1800 - 1000)');
        assert.strictEqual(r2.thinkingElapsedMs, undefined, 'r2 is the new open span');
        assert.strictEqual(r1.thinking, 'Grounding…', 'r1 keeps its own text, unmixed with r2');
    });

    test('freezeActiveReasoningEntries settles every still-open span and is a no-op once frozen', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertReasoningDelta(entries, 'Grounding…', 'r1', 100, { tokenCount: 1, now: 1_000 });
        const frozen = freezeActiveReasoningEntries(entries, 1_400);
        assert.strictEqual(frozen[0].thinkingElapsedMs, 400);

        const frozenAgain = freezeActiveReasoningEntries(frozen, 9_999);
        assert.strictEqual(frozenAgain, frozen, 'idempotent — an already-frozen entry is left untouched, same array identity');
    });

    test('freezeActiveReasoningEntries ignores non-reasoning entries', () => {
        const entries: TimelineEntry[] = [
            { id: 'seq:0', seq: 0, ts: 100, kind: 'read', status: 'active', target: 'a.py' },
        ];
        const frozen = freezeActiveReasoningEntries(entries, 1_400);
        assert.strictEqual(frozen, entries, 'no reasoning entries present — untouched');
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

    // ── Cell — order-agnostic correlation by cell:{iteration} ref ───────────

    function cellIteration(over: Partial<CellIterationShape> & { iteration: number }): CellIterationShape {
        return { tools: [], pty: [], diffs: [], ...over };
    }

    test('cell body upserts and merges without duplicating the entry', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertCellBody(entries, 'cell:0', cellIteration({
            iteration: 0, tools: [{ tool_name: 'run_terminal', args_scrubbed: {} }],
        }), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].kind, 'cell');
        assert.strictEqual(entries[0].target, 'run_terminal');
        assert.strictEqual(entries[0].status, 'active');

        entries = upsertCellBody(entries, 'cell:0', cellIteration({
            iteration: 0,
            tools: [{ tool_name: 'run_terminal', args_scrubbed: {} }],
            pty: ['$ pytest', '2 passed'],
        }), 100);
        assert.strictEqual(entries.length, 1);
        assert.deepStrictEqual(entries[0].cell?.pty, ['$ pytest', '2 passed']);
    });

    test('cell: marker arrives first, body attaches by cell:{iteration} ref', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({
            seq: 4, kind: 'cell', target: 'run_terminal', ref: 'cell:2', metric: 'iteration 3',
        }));
        entries = upsertCellBody(entries, 'cell:2', cellIteration({ iteration: 2 }), 100);

        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'cell:2');
        assert.strictEqual(entries[0].seq, 4);
        assert.strictEqual(entries[0].status, 'active');
        assert.ok(entries[0].cell);
        assert.strictEqual(entries[0].cell!.iteration, 2);
    });

    test('cell: body arrives first, marker resolves it (no duplicate)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertCellBody(entries, 'cell:2', cellIteration({ iteration: 2 }), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, Number.POSITIVE_INFINITY);
        assert.strictEqual(entries[0].status, 'active');

        entries = upsertActivityMarker(entries, marker({
            seq: 4, kind: 'cell', target: 'run_terminal', ref: 'cell:2', metric: 'iteration 3',
        }));
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, 4);
        assert.ok(entries[0].cell, 'cell body must survive the marker merge');
    });

    test('two different iterations never collide (distinct refs)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertCellBody(entries, 'cell:0', cellIteration({ iteration: 0 }), 100);
        entries = upsertCellBody(entries, 'cell:1', cellIteration({ iteration: 1 }), 100);
        assert.strictEqual(entries.length, 2);
        assert.deepStrictEqual(entries.map(e => e.id).sort(), ['cell:0', 'cell:1']);
    });

    // ── Execution detail — order-agnostic correlation by execution-id ref ────

    function execDetail(over: Partial<ExecutionDetailShape> = {}): ExecutionDetailShape {
        return { source: 'devcontainer', truncated: false, ...over };
    }

    test('a ref-carrying command marker starts active (unlike a ref-less one)', () => {
        const entries = upsertActivityMarker([], marker({
            seq: 6, kind: 'command', target: 'pytest -q', ref: 'exec-1',
        }));
        assert.strictEqual(entries[0].status, 'active');
    });

    test('a ref-less command marker (e.g. blocked) still settles immediately', () => {
        const entries = upsertActivityMarker([], marker({
            seq: 6, kind: 'command', target: 'rm -rf /', metric: 'denied',
        }));
        assert.strictEqual(entries[0].status, 'done');
        assert.strictEqual(entries[0].id, 'seq:6');
    });

    test('execution: marker arrives first, detail attaches by ref and resolves to done', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertActivityMarker(entries, marker({
            seq: 6, kind: 'command', target: 'pytest -q', ref: 'exec-1',
        }));
        assert.strictEqual(entries[0].status, 'active');

        entries = upsertExecutionBody(entries, 'exec-1', execDetail({ exit_code: 0, stdout: '2 passed' }), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'exec-1');
        assert.strictEqual(entries[0].seq, 6);
        assert.strictEqual(entries[0].status, 'done');
        assert.strictEqual(entries[0].execution?.stdout, '2 passed');
    });

    test('execution: detail arrives first, marker resolves it without touching the already-final status', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionBody(entries, 'exec-1', execDetail({ exit_code: 1, stderr: 'boom' }), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, Number.POSITIVE_INFINITY);
        assert.strictEqual(entries[0].status, 'failed');

        entries = upsertActivityMarker(entries, marker({
            seq: 6, kind: 'command', target: 'pytest -q', ref: 'exec-1',
        }));
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].seq, 6);
        // The merge path must NOT clobber a status the detail already resolved.
        assert.strictEqual(entries[0].status, 'failed');
        assert.ok(entries[0].execution, 'execution body must survive the marker merge');
    });

    test('non-zero exit code resolves to failed', () => {
        const entries = upsertExecutionBody([], 'exec-2', execDetail({ exit_code: 2 }), 100);
        assert.strictEqual(entries[0].status, 'failed');
    });

    test('a reported error resolves to failed even with no exit code', () => {
        const entries = upsertExecutionBody([], 'exec-3', execDetail({ error: 'adapter fault' }), 100);
        assert.strictEqual(entries[0].status, 'failed');
        assert.strictEqual(entries[0].execution?.exit_code, undefined);
    });

    test('two different executions never collide (distinct refs)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionBody(entries, 'exec-1', execDetail({ exit_code: 0 }), 100);
        entries = upsertExecutionBody(entries, 'exec-2', execDetail({ exit_code: 0 }), 100);
        assert.strictEqual(entries.length, 2);
        assert.deepStrictEqual(entries.map(e => e.id).sort(), ['exec-1', 'exec-2']);
    });

    // ── Live execution chunks (DEBT-134) — accumulate, clamp, then get replaced ──

    test('CHUNK1: a chunk with no prior entry creates an active placeholder', () => {
        const entries = upsertExecutionChunk([], 'exec-5', 'stdout', 'building…', 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].id, 'exec-5');
        assert.strictEqual(entries[0].status, 'active');
        assert.strictEqual(entries[0].execution?.stdout, 'building…');
    });

    test('CHUNK1: successive chunks accumulate onto the same field in order', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionChunk(entries, 'exec-5', 'stdout', 'foo', 100);
        entries = upsertExecutionChunk(entries, 'exec-5', 'stdout', 'bar', 100);
        entries = upsertExecutionChunk(entries, 'exec-5', 'stderr', 'warn', 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].execution?.stdout, 'foobar');
        assert.strictEqual(entries[0].execution?.stderr, 'warn');
        assert.strictEqual(entries[0].status, 'active');
    });

    test('CHUNK3: the terminal detail REPLACES accumulated chunk text, never appends to it', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionChunk(entries, 'exec-5', 'stdout', 'partial-output-that-should-vanish', 100);
        entries = upsertExecutionBody(entries, 'exec-5', execDetail({ exit_code: 0, stdout: 'final authoritative output' }), 100);
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].execution?.stdout, 'final authoritative output');
        assert.ok(!entries[0].execution?.stdout?.includes('partial-output-that-should-vanish'));
        assert.strictEqual(entries[0].status, 'done');
    });

    test('a chunk arriving AFTER the entry has already settled is a stale no-op', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionBody(entries, 'exec-6', execDetail({ exit_code: 0, stdout: 'settled' }), 100);
        entries = upsertExecutionChunk(entries, 'exec-6', 'stdout', 'late-straggler', 100);
        assert.strictEqual(entries[0].execution?.stdout, 'settled');
        assert.strictEqual(entries[0].status, 'done');
    });

    test('CHUNK5: the client-side retention ring clamps accumulated text and preserves both ends', () => {
        let entries: TimelineEntry[] = [];
        // MAX_LIVE_EXEC_FIELD_CHARS is 4_000 — push well past it across many chunks.
        for (let i = 0; i < 200; i++) {
            entries = upsertExecutionChunk(entries, 'exec-7', 'stdout', `chunk-${i}-`.padEnd(50, 'x'), 100);
        }
        const stdout = entries[0].execution?.stdout ?? '';
        assert.ok(stdout.length <= 4_100, `expected a clamped length, got ${stdout.length}`);
        assert.ok(stdout.startsWith('chunk-0-'), 'head must survive the clamp');
        assert.ok(stdout.includes('chunk-199-'), 'tail must survive the clamp');
        assert.ok(stdout.includes('chars truncated'), 'the clamp must be visibly marked, not silent');
    });

    test('two different live executions never collide (distinct refs)', () => {
        let entries: TimelineEntry[] = [];
        entries = upsertExecutionChunk(entries, 'exec-8', 'stdout', 'a', 100);
        entries = upsertExecutionChunk(entries, 'exec-9', 'stdout', 'b', 100);
        assert.strictEqual(entries.length, 2);
        assert.deepStrictEqual(entries.map(e => e.id).sort(), ['exec-8', 'exec-9']);
    });

    // ── Pure-on-inputs guarantee ──────────────────────────────────────────────

    test('every upsert returns a NEW array — never mutates the input', () => {
        const before: TimelineEntry[] = [];
        const after = upsertActivityMarker(before, marker({ seq: 0, kind: 'understanding' }));
        assert.notStrictEqual(before, after);
        assert.strictEqual(before.length, 0);
    });

    // ── Persistence — reasoning dropped, everything else survives ───────────

    test('stripReasoningForPersist drops reasoning entries, keeps everything else', () => {
        const entries: TimelineEntry[] = [
            { id: 'seq:0', seq: 0, ts: 100, kind: 'understanding', status: 'done' },
            { id: 'r1', seq: 1, ts: 101, kind: 'reasoning', status: 'active', thinking: 'secret chain of thought' },
            { id: 'a.py', seq: 2, ts: 102, kind: 'diff', status: 'done', target: 'a.py' },
        ];
        const persisted = stripReasoningForPersist(entries);
        assert.deepStrictEqual(persisted.map(e => e.kind), ['understanding', 'diff']);
        assert.ok(!persisted.some(e => 'thinking' in e && e.thinking));
    });
});
