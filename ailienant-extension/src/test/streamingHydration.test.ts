/**
 * Streaming-AST hydration contract tests (Phase 7.17.1).
 *
 * Covers the two webview-side primitives that keep progressive highlighting from
 * thrashing React reconciliation:
 *   1. `mergeStreamEmits` — the Copy-on-Write fold that coalesces a frame's worth
 *      of per-line token pushes while preserving the reference of every untouched
 *      line array (the precondition for the CodeLine memo to skip it).
 *   2. `codeLineEqual` — the memo comparator that decides whether a rendered code
 *      line can be reused.
 *
 * Host-agnostic: no VS Code API, no DOM, no grammar engine. Both units are pure.
 */
import * as assert from 'assert';
import { mergeStreamEmits, type StreamLineEmit } from '../workspace/utils/streamTokenBuffer';
import { codeLineEqual } from '../workspace/components/MarkdownRenderer';
import type { ASTToken } from '../shared/config';

/** A distinct, identity-comparable token array for line `n` of block `b`. */
function ast(label: string): ASTToken[] {
    return [{ type: 'scope.test', content: label }];
}

suite('mergeStreamEmits — Copy-on-Write coalescing', function () {

    test('M1 — batch merge equals sequential merge (multi-block, multi-line)', () => {
        const emits: StreamLineEmit[] = [
            { block_seq: 0, line_index: 0, ast: ast('b0l0') },
            { block_seq: 0, line_index: 1, ast: ast('b0l1') },
            { block_seq: 1, line_index: 0, ast: ast('b1l0') },
            { block_seq: 0, line_index: 2, ast: ast('b0l2') },
        ];
        const batched = mergeStreamEmits(undefined, emits);

        // Sequential application, one emit at a time, must land in the same slots.
        let seq: Record<number, ASTToken[][]> = {};
        for (const e of emits) { seq = mergeStreamEmits(seq, [e]); }

        assert.deepStrictEqual(batched, seq);
        assert.strictEqual(batched[0].length, 3, 'block 0 has 3 lines');
        assert.strictEqual(batched[1].length, 1, 'block 1 has 1 line');
        assert.strictEqual(batched[0][2][0].content, 'b0l2');
        assert.strictEqual(batched[1][0][0].content, 'b1l0');
    });

    test('M2 — untouched line arrays keep their EXACT reference (the memo invariant)', () => {
        // Seed block 0 with lines 0..5.
        const seed: StreamLineEmit[] = [];
        for (let i = 0; i <= 5; i++) { seed.push({ block_seq: 0, line_index: i, ast: ast(`seed${i}`) }); }
        const prev = mergeStreamEmits(undefined, seed);

        // Now update only line 5.
        const newL5 = ast('updated5');
        const next = mergeStreamEmits(prev, [{ block_seq: 0, line_index: 5, ast: newL5 }]);

        // Untouched lines 0..4 must be the very same array instances.
        for (let i = 0; i <= 4; i++) {
            assert.strictEqual(next[0][i], prev[0][i], `line ${i} reference must be preserved`);
        }
        // The touched line is the new array; the block array + dictionary are fresh.
        assert.strictEqual(next[0][5], newL5, 'touched line is the new ast');
        assert.notStrictEqual(next[0][5], prev[0][5], 'touched line ref changed');
        assert.notStrictEqual(next[0], prev[0], 'block array is a fresh clone');
        assert.notStrictEqual(next, prev, 'dictionary is a fresh clone');
    });

    test('M3 — a block updated twice in one batch is cloned only once', () => {
        const prev = mergeStreamEmits(undefined, [{ block_seq: 0, line_index: 0, ast: ast('a') }]);
        // Two emits into block 0 in the same batch.
        const next = mergeStreamEmits(prev, [
            { block_seq: 0, line_index: 1, ast: ast('b') },
            { block_seq: 0, line_index: 2, ast: ast('c') },
        ]);
        // Both writes landed and the seeded line 0 reference survived (one clone, not two).
        assert.strictEqual(next[0][0], prev[0][0], 'seeded line 0 reference preserved');
        assert.strictEqual(next[0][1][0].content, 'b');
        assert.strictEqual(next[0][2][0].content, 'c');
    });

    test('M4 — empty batch is a no-op (returns the existing record)', () => {
        const prev = mergeStreamEmits(undefined, [{ block_seq: 0, line_index: 0, ast: ast('a') }]);
        const next = mergeStreamEmits(prev, []);
        assert.strictEqual(next, prev, 'empty batch returns the same reference');
    });

    test('M5 — undefined existing + empty batch yields a fresh empty record', () => {
        const next = mergeStreamEmits(undefined, []);
        assert.deepStrictEqual(next, {});
    });
});

suite('codeLineEqual — CodeLine memo comparator', function () {

    test('C1 — equal when leadingNewline, text and tokens-ref all match', () => {
        const tokens = ast('x');
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens, leadingNewline: true },
                { text: 'foo', tokens, leadingNewline: true },
            ),
            true,
        );
    });

    test('C2 — not equal when the tail line text grows', () => {
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens: undefined, leadingNewline: false },
                { text: 'foob', tokens: undefined, leadingNewline: false },
            ),
            false,
        );
    });

    test('C3 — not equal when a line flips from plain to painted (tokens ref appears)', () => {
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens: undefined, leadingNewline: true },
                { text: 'foo', tokens: ast('foo'), leadingNewline: true },
            ),
            false,
        );
    });

    test('C4 — not equal when the tokens array is a different reference (same content)', () => {
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens: ast('foo'), leadingNewline: true },
                { text: 'foo', tokens: ast('foo'), leadingNewline: true },
            ),
            false,
        );
    });

    test('C5 — not equal when only leadingNewline differs', () => {
        const tokens = ast('x');
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens, leadingNewline: false },
                { text: 'foo', tokens, leadingNewline: true },
            ),
            false,
        );
    });
});
