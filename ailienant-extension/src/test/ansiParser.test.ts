/**
 * Phase 7.11.6 (ADR-706 §4.5f) — ANSI SGR parser contract tests.
 *
 * Verifies the parser handles the SGR subset we promise to render in the
 * Rich Tool Chip mini-terminal: 16-color FG/BG, bold/italic/underline,
 * reset, 24-bit truecolor, and — critically — chunk-truncated escape
 * sequences (the W3 streaming-resilience guarantee).
 */
import * as assert from 'assert';
import { INITIAL_STATE, parseAnsi } from '../workspace/utils/ansiParser';

suite('Phase 7.11.6 — ansiParser', () => {

    test('standard 8-color foreground emits the right class', () => {
        const { runs, state } = parseAnsi('\x1b[31mred\x1b[0m', INITIAL_STATE);
        // One styled "red" run; the reset doesn't produce any text but clears
        // the trailing state.
        const reds = runs.filter(r => r.text === 'red');
        assert.strictEqual(reds.length, 1, 'expected exactly one "red" run');
        assert.ok(reds[0].classes.includes('ansi-red'));
        // Trailing state is reset.
        assert.strictEqual(state.fg, undefined);
        assert.strictEqual(state.bold, false);
    });

    test('bright color (90-97) emits the bright class variant', () => {
        const { runs } = parseAnsi('\x1b[91mbright\x1b[0m', INITIAL_STATE);
        const bright = runs.find(r => r.text === 'bright');
        assert.ok(bright, 'no run with text "bright"');
        assert.ok(bright!.classes.includes('ansi-bright-red'));
    });

    test('bold + underline + italic combine', () => {
        const { runs } = parseAnsi('\x1b[1;3;4mbold\x1b[0m', INITIAL_STATE);
        const styled = runs.find(r => r.text === 'bold');
        assert.ok(styled, 'no styled run');
        for (const cls of ['ansi-bold', 'ansi-italic', 'ansi-underline']) {
            assert.ok(
                styled!.classes.includes(cls),
                `class ${cls} missing from ${JSON.stringify(styled!.classes)}`,
            );
        }
    });

    test('reset (\\x1b[0m) clears every flag', () => {
        // Style something, reset, then write plain text — the plain text run
        // must have NO classes.
        const { runs } = parseAnsi('\x1b[31m\x1b[1mred-bold\x1b[0mplain', INITIAL_STATE);
        const plain = runs.find(r => r.text === 'plain');
        assert.ok(plain, 'no plain run');
        assert.deepStrictEqual(plain!.classes, [], 'plain run must have NO classes after reset');
    });

    test('truncated escape carries over to the next chunk (streaming W3)', () => {
        // Tool emits `\x1b[31` at chunk-1 boundary (the terminator `m` lands
        // in chunk-2). The parser must hold the partial escape so the color
        // doesn't bleed and the partial chars don't leak as text.
        const r1 = parseAnsi('\x1b[31', INITIAL_STATE);
        assert.strictEqual(r1.runs.length, 0, 'no runs should emit from a pure partial');
        assert.strictEqual(r1.state.partial_escape, '\x1b[31');

        // Feed the rest of the sequence + content.
        const r2 = parseAnsi('mhello\x1b[0m', r1.state);
        const styled = r2.runs.find(r => r.text === 'hello');
        assert.ok(styled, 'no run with text "hello" after partial-then-rest');
        assert.ok(styled!.classes.includes('ansi-red'));
        // Partial drained on success.
        assert.strictEqual(r2.state.partial_escape, '');
    });

    test('24-bit truecolor emits inline style, not a class', () => {
        const { runs } = parseAnsi('\x1b[38;2;255;128;0morange\x1b[0m', INITIAL_STATE);
        const styled = runs.find(r => r.text === 'orange');
        assert.ok(styled, 'no run with text "orange"');
        assert.ok(styled!.style, 'expected inline style for truecolor');
        assert.strictEqual(styled!.style!.color, 'rgb(255, 128, 0)');
        // No FG class should be set when truecolor takes over.
        const hasFgClass = styled!.classes.some(c => c.startsWith('ansi-') && !c.startsWith('ansi-bold')
            && !c.startsWith('ansi-italic') && !c.startsWith('ansi-underline')
            && !c.startsWith('ansi-dim') && !c.startsWith('ansi-bg-'));
        assert.ok(!hasFgClass, `truecolor should suppress FG class, got ${JSON.stringify(styled!.classes)}`);
    });

    test('non-SGR CSI sequences are silently dropped (no text leak)', () => {
        // Cursor-clear sequences should NOT produce stray text in the output.
        const { runs } = parseAnsi('before\x1b[2Jafter', INITIAL_STATE);
        const text = runs.map(r => r.text).join('');
        assert.strictEqual(text, 'beforeafter', `unexpected text: ${text}`);
        // No `2J` characters leaked.
        assert.ok(!text.includes('2J'));
    });
});
