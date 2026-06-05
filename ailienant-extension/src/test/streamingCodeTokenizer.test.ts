/**
 * StreamingCodeTokenizer contract tests.
 *
 * Verifies the O(1) incremental tokenization invariants and the three
 * hardening requirements from the architectural critique:
 *   1. No full-buffer re-lex per token (each line tokenized exactly once).
 *   2. FIFO drain of lines that arrive before async init resolves.
 *   3. Chunk-boundary safety: fence detection across split WS chunks.
 *   4. Memory-leak / zombie guard: no callbacks after reset().
 *
 * Host-agnostic: no VS Code API, no DOM, no real grammar engine — the
 * LineTokenizer factory is injected and replaced with synchronous stubs.
 */
import * as assert from 'assert';
import { StreamingCodeTokenizer } from '../core/StreamingCodeTokenizer';
import type { LineTokenizer } from '../core/GrammarLexer';
import type { ASTToken } from '../shared/config';
import { extractCodeBlocks } from '../workspace/utils/StreamingMarkdownParser';

/** A synchronous stub LineTokenizer that records every call. */
function makeStubTokenizer(
    label: string,
): { tokenizer: LineTokenizer; calls: { line: string; label: string }[] } {
    const calls: { line: string; label: string }[] = [];
    const tokenizer: LineTokenizer = {
        tokenizeLine(line: string): ASTToken[] {
            calls.push({ line, label });
            return [{ type: 'stub.scope', content: line }];
        },
    };
    return { tokenizer, calls };
}

/** Build a synchronous CreateLineTokenizer that always returns the given tokenizer. */
function syncFactory(tok: LineTokenizer | undefined): (lang: string) => Promise<LineTokenizer | undefined> {
    return () => Promise.resolve(tok);
}

/** Feed an entire string char-by-char (simulating max-granularity chunking). */
function feedChars(tokenizer: StreamingCodeTokenizer, text: string): void {
    for (const ch of text) { tokenizer.push(ch); }
}

suite('StreamingCodeTokenizer — incremental streaming tokenization contract', function () {
    this.timeout(30_000);

    // ── Core correctness ──────────────────────────────────────────────────

    test('T1 — emits one LineEmit per completed code line in the correct order', async () => {
        const { tokenizer: stub } = makeStubTokenizer('T1');
        const emits: { block_seq: number; line_index: number; content: string }[] = [];
        const sct = new StreamingCodeTokenizer(syncFactory(stub), (e) => {
            emits.push({ block_seq: e.block_seq, line_index: e.line_index, content: e.ast[0]?.content ?? '' });
        });
        const text = '```python\nline0\nline1\nline2\n```\n';
        // Async factory → let the microtask queue drain before asserting.
        sct.push(text);
        await Promise.resolve();
        await Promise.resolve();
        assert.strictEqual(emits.length, 3, 'expected 3 line emits');
        assert.deepStrictEqual(emits.map(e => e.line_index), [0, 1, 2]);
        assert.deepStrictEqual(emits.map(e => e.content), ['line0', 'line1', 'line2']);
        assert.ok(emits.every(e => e.block_seq === 0), 'all in block_seq 0');
    });

    test('T2 — block_seq increments for each distinct fenced block', async () => {
        const { tokenizer: stub } = makeStubTokenizer('T2');
        const seqs: number[] = [];
        const sct = new StreamingCodeTokenizer(syncFactory(stub), (e) => { seqs.push(e.block_seq); });
        sct.push('```ts\na\n```\nprose\n```py\nb\n```\n');
        await Promise.resolve(); await Promise.resolve();
        const unique = [...new Set(seqs)];
        assert.deepStrictEqual(unique, [0, 1], 'two distinct block_seqs');
    });

    test('T3 — unknown/empty lang emits nothing (factory returns undefined)', async () => {
        const emits: unknown[] = [];
        const sct = new StreamingCodeTokenizer(syncFactory(undefined), (e) => { emits.push(e); });
        sct.push('```unknownlang\nsome code\n```\n');
        await Promise.resolve(); await Promise.resolve();
        assert.strictEqual(emits.length, 0, 'no emits for unsupported lang');
    });

    test('T4 — no re-lex: tokenizeLine called exactly once per code line', async () => {
        const { tokenizer: stub, calls } = makeStubTokenizer('T4');
        const sct = new StreamingCodeTokenizer(syncFactory(stub), () => {});
        sct.push('```py\nfoo\nbar\nbaz\n```\n');
        await Promise.resolve(); await Promise.resolve();
        assert.strictEqual(calls.length, 3, 'exactly 3 tokenizeLine calls, one per line');
    });

    test('T5 — ordinal/line alignment matches extractCodeBlocks result', async () => {
        const text = '```python\nprint(1)\nprint(2)\n```\nsome prose\n```ts\nconst x = 1;\n```\n';
        const blocks = extractCodeBlocks(text);
        const expectedLines0 = blocks[0].code.split('\n');
        const expectedLines1 = blocks[1].code.split('\n');

        const emits: { block_seq: number; line_index: number; line: string }[] = [];
        const { tokenizer: stub } = makeStubTokenizer('T5');
        const sct = new StreamingCodeTokenizer(syncFactory(stub), (e) => {
            emits.push({ block_seq: e.block_seq, line_index: e.line_index, line: e.ast[0]?.content ?? '' });
        });
        sct.push(text);
        await Promise.resolve(); await Promise.resolve();

        const block0 = emits.filter(e => e.block_seq === 0);
        const block1 = emits.filter(e => e.block_seq === 1);
        assert.deepStrictEqual(block0.map(e => e.line), expectedLines0);
        assert.deepStrictEqual(block1.map(e => e.line), expectedLines1);
    });

    // ── Hardening critique #1 — FIFO drain of pending lines ───────────────

    test('H1 — lines arriving before async init are drained FIFO in correct order', async () => {
        let resolve!: (tok: LineTokenizer | undefined) => void;
        const pending = new Promise<LineTokenizer | undefined>(r => { resolve = r; });
        const { tokenizer: stub } = makeStubTokenizer('H1');

        const emits: { line_index: number; content: string }[] = [];
        const sct = new StreamingCodeTokenizer(
            () => pending,
            (e) => { emits.push({ line_index: e.line_index, content: e.ast[0]?.content ?? '' }); },
        );

        // Feed 3 code lines BEFORE the factory resolves.
        sct.push('```python\nline_a\nline_b\nline_c\n');
        assert.strictEqual(emits.length, 0, 'no emits before factory resolves');

        // Resolve the factory — FIFO drain should fire.
        resolve(stub);
        await Promise.resolve(); await Promise.resolve(); await Promise.resolve();

        assert.strictEqual(emits.length, 3, 'all 3 pending lines emitted after resolve');
        assert.deepStrictEqual(emits.map(e => e.content), ['line_a', 'line_b', 'line_c'], 'FIFO order');
        assert.deepStrictEqual(emits.map(e => e.line_index), [0, 1, 2], 'correct line indices');
    });

    // ── Hardening critique #2 — chunk-boundary safety ─────────────────────

    test('H2 — fence and lines split across push() calls produce identical result', async () => {
        const fullText = '```python\nhello\nworld\n```\n';
        const { tokenizer: stub1 } = makeStubTokenizer('H2-single');
        const single: { li: number; c: string }[] = [];
        const sct1 = new StreamingCodeTokenizer(syncFactory(stub1), (e) => {
            single.push({ li: e.line_index, c: e.ast[0]?.content ?? '' });
        });
        sct1.push(fullText);
        await Promise.resolve(); await Promise.resolve();

        // Split: fence opener split across two pushes, body across several more.
        const { tokenizer: stub2 } = makeStubTokenizer('H2-split');
        const split: { li: number; c: string }[] = [];
        const sct2 = new StreamingCodeTokenizer(syncFactory(stub2), (e) => {
            split.push({ li: e.line_index, c: e.ast[0]?.content ?? '' });
        });
        sct2.push('``');     sct2.push('`python');
        sct2.push('\nhell'); sct2.push('o\n');
        sct2.push('wor');    sct2.push('ld\n');
        sct2.push('``');     sct2.push('`\n');
        await Promise.resolve(); await Promise.resolve();

        assert.deepStrictEqual(split, single, 'split-chunk result == single-chunk result');
    });

    test('H2b — \\r\\n line endings produce same output as \\n', async () => {
        const { tokenizer: stub } = makeStubTokenizer('H2b');
        const emits: string[] = [];
        const sct = new StreamingCodeTokenizer(syncFactory(stub), (e) => {
            emits.push(e.ast[0]?.content ?? '');
        });
        sct.push('```py\r\nline1\r\nline2\r\n```\r\n');
        await Promise.resolve(); await Promise.resolve();
        assert.deepStrictEqual(emits, ['line1', 'line2'], '\\r stripped, lines identical');
    });

    // ── Hardening critique #3 — disconnect / zombie guard ─────────────────

    test('H3 — reset() prevents any callbacks after it is called', async () => {
        let resolve!: (tok: LineTokenizer | undefined) => void;
        const pending = new Promise<LineTokenizer | undefined>(r => { resolve = r; });
        const { tokenizer: stub } = makeStubTokenizer('H3');

        const postResetEmits: unknown[] = [];
        const sct = new StreamingCodeTokenizer(
            () => pending,
            (e) => { postResetEmits.push(e); },
        );

        // Start a block, queue some lines.
        sct.push('```python\nline1\nline2\n');
        // Simulate WS disconnect — reset before the factory resolves.
        sct.reset();

        // Now resolve the factory — the drain should be suppressed.
        resolve(stub);
        await Promise.resolve(); await Promise.resolve(); await Promise.resolve();

        assert.strictEqual(postResetEmits.length, 0, 'no callbacks after reset()');
    });

    test('H3b — push() after reset() starts a fresh block with block_seq 0', async () => {
        const { tokenizer: stub } = makeStubTokenizer('H3b');
        const emits: number[] = [];
        const sct = new StreamingCodeTokenizer(syncFactory(stub), (e) => { emits.push(e.block_seq); });
        sct.push('```py\naaa\n```\n');
        sct.reset();
        sct.push('```py\nbbb\n```\n');
        await Promise.resolve(); await Promise.resolve();
        assert.deepStrictEqual(emits, [0], 'after reset, block_seq starts at 0 again');
    });
});
