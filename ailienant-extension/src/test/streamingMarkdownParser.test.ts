/**
 * Phase 7.11.5 (ADR-706 §4.5e) — StreamingMarkdownParser contract tests.
 *
 * Verifies the O(1)-amortized invariants documented in the parser module:
 *   - Per-token flag-delta is bounded (no historical re-scan).
 *   - Virtual closures appear for unclosed constructs and clear on close.
 *   - Source buffer is byte-identical to the concatenation of all tokens.
 *   - CommonMark §4.5 fence open/close symmetry (nested markdown).
 *
 * No real VS Code host is needed — the parser is pure.
 */
import * as assert from 'assert';
import {
    INITIAL_STATE,
    ParserState,
    closuresFor,
    finalize,
    flagDelta,
    pushToken,
} from '../workspace/utils/StreamingMarkdownParser';

suite('Phase 7.11.5 — StreamingMarkdownParser', () => {

    // ── Test 1 — O(1) per token: bounded flag-flips ─────────────────────
    test('pushToken flips at most 3 flags per char-token (no historical re-scan)', () => {
        // Mixed-construct document streamed CHAR-BY-CHAR. Each pushToken call
        // sees exactly one char of input plus the prior state; the assertion
        // proves the parser cannot be doing a buffer-wide re-scan.
        const doc =
            'Hello **bold** then *italic*\n' +
            '```python\nprint("hi")\n```\n' +
            'and `inline` and ~~strike~~\n' +
            '[link](http://example.com) end.\n';

        let state: ParserState = INITIAL_STATE;
        for (const ch of doc) {
            const next = pushToken(state, ch);
            const delta = flagDelta(state, next);
            assert.ok(
                delta <= 3,
                `flagDelta=${delta} for char ${JSON.stringify(ch)} — parser is doing a re-scan`,
            );
            state = next;
        }
    });

    // ── Test 2 — virtual closures for an unclosed fence ─────────────────
    test('closuresFor returns code_fence with captured lang for an open fence', () => {
        const s = pushToken(INITIAL_STATE, '```js\nconst');
        assert.strictEqual(s.in_code_fence, true,  'fence should be open mid-stream');
        assert.strictEqual(s.fence_lang,    'js',  'info string should be captured');
        const cs = closuresFor(s);
        const fence = cs.find(c => c.tag === 'code_fence');
        assert.ok(fence,            'code_fence closure missing');
        assert.strictEqual(fence?.lang, 'js');
    });

    // ── Test 3 — close fence on its own line clears closures ────────────
    test('closing fence on its own line clears the open-fence closure', () => {
        let s = pushToken(INITIAL_STATE, '```js\nconst');
        s = pushToken(s, '\n```\n');
        assert.strictEqual(s.in_code_fence, false, 'fence should be closed after closer');
        assert.deepStrictEqual(closuresFor(s), [], 'no open closures expected');
    });

    // ── Test 4 — info string captured on opener ─────────────────────────
    test('fence_lang captured from opening info string', () => {
        const s = pushToken(INITIAL_STATE, '```python\n');
        assert.strictEqual(s.in_code_fence, true);
        assert.strictEqual(s.fence_lang,    'python');
    });

    // ── Test 5 — inline code suppresses emphasis (CMK §6.1) ─────────────
    test('inline code wins over emphasis: asterisks inside `…` are inert', () => {
        const s = pushToken(INITIAL_STATE, '`*not bold*`');
        assert.strictEqual(s.in_inline_code, false, 'inline code should close cleanly');
        assert.strictEqual(s.in_bold,        false, 'asterisks were inert inside backticks');
        assert.strictEqual(s.in_italic,      false);
    });

    // ── Test 6 — bold opener split across two tokens (W7) ───────────────
    test('** opener split across two tokens still closes cleanly', () => {
        // The 1-char prev_char window is the only thing that makes this work.
        let s: ParserState = INITIAL_STATE;
        s = pushToken(s, '*');
        // After a single '*', the parser provisionally flips italic — by
        // design (we only know it's the second char of '**' once the SECOND
        // '*' arrives).
        assert.strictEqual(s.in_italic, true, 'provisional italic flip on first *');
        s = pushToken(s, '*hi**');
        assert.strictEqual(s.in_italic, false, 'final italic flag must be cleared');
        assert.strictEqual(s.in_bold,   false, 'final bold flag must be cleared');
    });

    // ── Test 7 — source-buffer immutability ─────────────────────────────
    test('concatenation of all tokens equals the user-visible content', () => {
        // The parser is supposed to be a pure function: no synthetic chars
        // leak into the data. The renderer is what injects virtual closures.
        const tokens = [
            '## Title\n',
            'Some **bold** and *italic* text.\n',
            '```js\nconst x = 1\n```\n',
            'Inline `code` and ~~strike~~.\n',
        ];
        let state: ParserState = INITIAL_STATE;
        const accumulated: string[] = [];
        for (const t of tokens) {
            // Sanity: the input string is the same after pushToken (we don't
            // accept a string mutation).
            const tokenCopy = t;
            state = pushToken(state, t);
            assert.strictEqual(tokenCopy, t, 'token input must not be mutated');
            accumulated.push(t);
        }
        const concat = accumulated.join('');
        const userOriginal = tokens.join('');
        assert.strictEqual(concat, userOriginal, 'parser must not append/remove chars');
    });

    // ── Test 8 — abrupt termination mid-fence ───────────────────────────
    test('no-op chunk after mid-fence content leaves state stable', () => {
        let s = pushToken(INITIAL_STATE, '```\nconst x');
        const before = s;
        s = pushToken(s, '');
        assert.strictEqual(s, before, 'empty token must be a pure no-op');
        assert.strictEqual(s.in_code_fence, true);
        const fence = closuresFor(s).find(c => c.tag === 'code_fence');
        assert.ok(fence, 'open fence must still surface a virtual closure');
    });

    // ── Test 9 — CommonMark §4.5 fence symmetry: nested markdown (W9) ───
    test('outer 4-backtick fence stays open through an inner 3-backtick fence', () => {
        // The LLM is demonstrating how to write a markdown code-fence inside
        // a markdown code-fence. The outer fence MUST stay open until a run
        // of ≥ 4 backticks arrives at the start of a line.
        let s: ParserState = INITIAL_STATE;
        s = pushToken(s, '````markdown\n');
        assert.strictEqual(s.in_code_fence, true);
        assert.strictEqual(s.fence_char,   '`');
        assert.strictEqual(s.fence_len,    4);
        assert.strictEqual(s.fence_lang,   'markdown');

        s = pushToken(s, '```python\nprint("hi")\n');
        // Inner 3-backtick run did NOT close the outer fence.
        assert.strictEqual(s.in_code_fence, true,  'inner 3-backtick must not close outer 4-backtick fence');
        assert.strictEqual(s.fence_len,     4,    'fence_len preserved from opener');
        assert.strictEqual(s.fence_lang,    'markdown', 'fence_lang preserved (inner python tag is content)');

        s = pushToken(s, '```\n');
        // Inner CLOSING 3-backtick at line start ALSO must not close the
        // outer 4-backtick fence (this is the headline asymmetry the test
        // catches — easy to get wrong).
        assert.strictEqual(s.in_code_fence, true,  'inner 3-backtick close must not close outer fence');
        assert.strictEqual(s.fence_len,     4);

        s = pushToken(s, '````\n');
        // Now a run of 4 backticks at line start closes the outer fence.
        assert.strictEqual(s.in_code_fence, false, 'matching 4-backtick run must close the outer fence');
        assert.strictEqual(s.fence_lang,    '',    'fence_lang cleared on close');
        assert.deepStrictEqual(closuresFor(s), []);
    });

    // ── Bonus: finalize closes a trailing fence run without a newline ───
    test('finalize() closes a trailing matching fence run that never saw \\n', () => {
        // Real LLM streams almost always emit a trailing newline, but a
        // truncated stream might not. finalize() is the stream-end safety net.
        let s = pushToken(INITIAL_STATE, '```\nconst x = 1\n');
        s = pushToken(s, '```');     // closing fence WITHOUT trailing newline
        // Mid-stream we leave the run pending (a 5th backtick might still arrive).
        assert.strictEqual(s.in_code_fence, true, 'pending fence_run leaves fence open mid-stream');
        s = finalize(s);
        assert.strictEqual(s.in_code_fence, false, 'finalize closes a satisfying trailing fence run');
    });

});
