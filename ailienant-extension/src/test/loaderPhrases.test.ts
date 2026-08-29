/**
 * loaderPhrases.ts — the loader's phrase pools for kinds with no concrete detail.
 *
 * Pure function tests, no JSDOM/React needed.
 */
import * as assert from 'assert';
import { hasPhrasePool, poolPhrase } from '../workspace/utils/loaderPhrases';

suite('13.1.9 — hasPhrasePool', () => {
    test('the three no-concrete-detail kinds have a pool', () => {
        assert.strictEqual(hasPhrasePool('understanding'), true);
        assert.strictEqual(hasPhrasePool('planning'), true);
        assert.strictEqual(hasPhrasePool('reviewing'), true);
    });

    test('kinds with real target data do not pool', () => {
        assert.strictEqual(hasPhrasePool('read'), false);
        assert.strictEqual(hasPhrasePool('tool'), false);
        assert.strictEqual(hasPhrasePool('retrieval'), false);
        assert.strictEqual(hasPhrasePool('command'), false);
    });
});

suite('13.1.9 — poolPhrase', () => {
    test('index 0 returns the same phrase timelineEntryLabel already uses for that kind', () => {
        assert.strictEqual(poolPhrase('understanding', 0), 'Understanding your request');
        assert.strictEqual(poolPhrase('planning', 0), 'Planning');
        assert.strictEqual(poolPhrase('reviewing', 0), 'Reviewing the plan');
    });

    test('wraps around once the index exceeds the pool length', () => {
        const first = poolPhrase('understanding', 0);
        const wrapped = poolPhrase('understanding', 4 /* pool has 4 entries */);
        assert.strictEqual(wrapped, first);
    });

    test('a kind with no pool returns undefined', () => {
        assert.strictEqual(poolPhrase('read', 0), undefined);
    });
});
