/**
 * Phase 7.17 — Checkpoint Gate (streaming progressive highlighting).
 *
 * Five load-bearing assertions for the 7.17 frontend contract. Does NOT re-run
 * the detailed suites from `streamingHydration.test.ts` or
 * `streamingCodeTokenizer.test.ts`; instead each row pins one architectural
 * invariant that would silently break the UX if violated.
 *
 *   STREAM1  — StreamingCodeTokenizer exposes push/reset (host-side seam intact).
 *   COW1     — mergeStreamEmits: updated line N has a fresh ref; lines 0..N-1 keep
 *              their EXACT original ref (the CodeLine memo's precondition — break this
 *              and the "Christmas tree" flicker returns).
 *   COW2     — two emits into the same block in one batch: block array cloned ONCE
 *              (clone-once-per-batch guard protects the untouched lines).
 *   NOOP1    — empty emit batch returns the same record reference (no spurious
 *              setMessages call / re-render).
 *   MEMO1    — codeLineEqual uses reference equality for tokens, not content equality:
 *              same ref → React skips the row; new ref → React reconciles the row,
 *              even when the token content is byte-identical.
 */
import * as assert from 'assert';
import { mergeStreamEmits, type StreamLineEmit } from '../workspace/utils/streamTokenBuffer';
import { codeLineEqual } from '../workspace/components/MarkdownRenderer';
import { StreamingCodeTokenizer } from '../core/StreamingCodeTokenizer';
import type { ASTToken } from '../shared/config';

/** Produce a distinct, identity-comparable token array. */
function ast(label: string): ASTToken[] {
    return [{ type: 'scope.gate', content: label }];
}

suite('Phase 7.17 — Checkpoint Gate (streaming progressive highlighting)', function () {

    // ── STREAM1 ────────────────────────────────────────────────────────────────

    test('STREAM1 — StreamingCodeTokenizer exposes push and reset', () => {
        const sct = new StreamingCodeTokenizer(
            () => Promise.resolve(undefined),
            () => undefined,
        );
        assert.strictEqual(typeof sct.push, 'function', 'push must be a method');
        assert.strictEqual(typeof sct.reset, 'function', 'reset must be a method');
    });

    // ── COW1 ───────────────────────────────────────────────────────────────────

    test('COW1 — updating line N leaves lines 0..N-1 with their exact original reference', () => {
        // Seed lines 0..4 into block 0.
        const seed: StreamLineEmit[] = [];
        for (let i = 0; i <= 4; i++) {
            seed.push({ block_seq: 0, line_index: i, ast: ast(`seed${i}`) });
        }
        const prev = mergeStreamEmits(undefined, seed);

        const newL4 = ast('updated4');
        const next = mergeStreamEmits(prev, [{ block_seq: 0, line_index: 4, ast: newL4 }]);

        // Lines 0..3 must be THE SAME ARRAY INSTANCE — reference equality.
        for (let i = 0; i <= 3; i++) {
            assert.strictEqual(
                next[0][i], prev[0][i],
                `line ${i} reference must survive an update to line 4`,
            );
        }
        // The touched line is the new array.
        assert.strictEqual(next[0][4], newL4, 'line 4 must be the new ast');
        // The block array and dictionary spine are fresh clones.
        assert.notStrictEqual(next[0], prev[0], 'block array must be a new clone');
        assert.notStrictEqual(next, prev, 'dictionary spine must be a new clone');
    });

    // ── COW2 ───────────────────────────────────────────────────────────────────

    test('COW2 — two emits into the same block clone that block array exactly once', () => {
        // Seed line 0; then emit lines 1 and 2 in the SAME batch.
        const prev = mergeStreamEmits(undefined, [{ block_seq: 0, line_index: 0, ast: ast('a') }]);
        const next = mergeStreamEmits(prev, [
            { block_seq: 0, line_index: 1, ast: ast('b') },
            { block_seq: 0, line_index: 2, ast: ast('c') },
        ]);

        // The clone-once guard means the seeded line 0 reference survived both writes.
        assert.strictEqual(
            next[0][0], prev[0][0],
            'seeded line 0 reference must survive a double-update in one batch',
        );
        assert.strictEqual(next[0][1][0].content, 'b', 'line 1 content correct');
        assert.strictEqual(next[0][2][0].content, 'c', 'line 2 content correct');
    });

    // ── NOOP1 ──────────────────────────────────────────────────────────────────

    test('NOOP1 — empty emit batch returns the same record reference (no re-render trigger)', () => {
        const prev = mergeStreamEmits(undefined, [{ block_seq: 0, line_index: 0, ast: ast('x') }]);
        const next = mergeStreamEmits(prev, []);
        assert.strictEqual(next, prev, 'empty batch must return the identical record reference');
    });

    // ── MEMO1 ──────────────────────────────────────────────────────────────────

    test('MEMO1 — codeLineEqual: reference equality for tokens is the discriminant, not content', () => {
        const ref = ast('same-content');

        // Same reference → equal (CodeLine skips reconciliation).
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens: ref, leadingNewline: false },
                { text: 'foo', tokens: ref, leadingNewline: false },
            ),
            true,
            'same tokens reference must be equal (React skips the row)',
        );

        // Different reference with byte-identical content → NOT equal (CodeLine reconciles).
        // This is the critical invariant: content equality is NOT sufficient; the memo
        // is reference-based so that mergeStreamEmits's CoW guarantee is the sole
        // source of truth about whether a row changed.
        assert.strictEqual(
            codeLineEqual(
                { text: 'foo', tokens: ast('same-content'), leadingNewline: false },
                { text: 'foo', tokens: ast('same-content'), leadingNewline: false },
            ),
            false,
            'different references must NOT be equal even when content is identical',
        );
    });
});
