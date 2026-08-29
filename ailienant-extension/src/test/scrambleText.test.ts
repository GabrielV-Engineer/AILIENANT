/**
 * scrambleText.ts — the loader's lead-character decode transition.
 *
 * Pure, deterministic function tests, no JSDOM/React needed.
 */
import * as assert from 'assert';
import { scrambleFrame, SCRAMBLE_TICKS } from '../workspace/utils/scrambleText';

suite('13.1.9 — scrambleFrame', () => {
    test('tick 0 replaces only the lead characters, leaving the rest untouched', () => {
        const frame = scrambleFrame('Getting oriented', 0);
        assert.strictEqual(frame.slice(2), 'tting oriented');
        assert.notStrictEqual(frame.slice(0, 2), 'Ge');
    });

    test('is deterministic — the same tick always produces the same frame', () => {
        assert.strictEqual(scrambleFrame('Reading foo.py', 1), scrambleFrame('Reading foo.py', 1));
    });

    test('different ticks produce different lead glyphs', () => {
        assert.notStrictEqual(scrambleFrame('Reading foo.py', 0), scrambleFrame('Reading foo.py', 1));
    });

    test('settles to the exact input text once tick reaches SCRAMBLE_TICKS', () => {
        assert.strictEqual(scrambleFrame('Planning', SCRAMBLE_TICKS), 'Planning');
        assert.strictEqual(scrambleFrame('Planning', SCRAMBLE_TICKS + 5), 'Planning');
    });

    test('a one-character string never throws and settles correctly', () => {
        assert.strictEqual(scrambleFrame('X', SCRAMBLE_TICKS), 'X');
        assert.strictEqual(scrambleFrame('X', 0).length, 1);
    });

    test('an empty string returns empty at every tick', () => {
        assert.strictEqual(scrambleFrame('', 0), '');
        assert.strictEqual(scrambleFrame('', SCRAMBLE_TICKS), '');
    });
});
