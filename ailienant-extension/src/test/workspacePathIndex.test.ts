/**
 * Phase 7.11.4 (ADR-706 §4.5d) — WorkspacePathIndex contract tests.
 *
 * Targets the pure trie + debounce paths. The real `bootstrap()` does a
 * workspace `findFiles` scan that depends on the test rig's open folder —
 * we cover its glue separately via manual smoke. These tests focus on the
 * data-structure invariants and the 500 ms debounced-flush contract.
 */
import * as assert from 'assert';
import { WorkspacePathIndex, extractMentions } from '../providers/workspacePathIndex';

suite('Phase 7.11.4 — WorkspacePathIndex', () => {

    // ── Test 1 — insert / query top-N round-trip ────────────────────────
    test('query returns files before folders, alphabetical within each group', () => {
        const idx = new WorkspacePathIndex();
        idx.insert('src/a.ts');
        idx.insert('src/b.ts');
        idx.insert('src/utils/x.ts');     // contributes a `src/utils` folder hit
        idx.insert('docs/x.md');

        const hits = idx.query('src/', 12);
        // Expect three rows: two leaf files + one folder ("utils").
        const paths = hits.map(h => `${h.kind}:${h.path}`);
        assert.deepStrictEqual(paths, [
            'file:src/a.ts',
            'file:src/b.ts',
            'folder:src/utils',
        ]);
    });

    // ── Test 2 — delete removes the leaf + prunes empty intermediates ───
    test('remove prunes empty intermediate folders', () => {
        const idx = new WorkspacePathIndex();
        idx.insert('src/only/leaf.ts');
        assert.strictEqual(idx.getSize(), 1);

        idx.remove('src/only/leaf.ts');
        assert.strictEqual(idx.getSize(), 0);
        // The intermediates are pruned — a subsequent query for `src/only`
        // returns nothing.
        const hits = idx.query('src/', 12);
        assert.deepStrictEqual(hits, []);
    });

    // ── Test 3 — fs-watcher events are batched within the debounce ──────
    test('enqueued adds are not visible until the debounced flush fires', async () => {
        const idx = new WorkspacePathIndex({ debounceMs: 30 });
        for (let i = 0; i < 10; i++) {
            idx.enqueueAdd(`src/file${i}.ts`);
        }
        // Pre-flush: the trie does NOT yet reflect the pending adds.
        assert.strictEqual(idx.getSize(), 0, 'no work should be done before debounce window elapses');

        await new Promise<void>((resolve) => setTimeout(resolve, 60));
        // Post-flush: all 10 adds applied in a single batch.
        assert.strictEqual(idx.getSize(), 10);
        const hits = idx.query('src/', 20);
        assert.strictEqual(hits.length, 10, 'all 10 paths must be queryable post-flush');
        idx.dispose();
    });

    // ── Test 4 — enumerateFolder caps + bail-out contract ───────────────
    test('enumerateFolder honors the 50-file cap and bails on > 200 entries', () => {
        const idx = new WorkspacePathIndex();
        // Populate a deep folder with 60 files — over the 50-file cap but
        // under the 200 give-up threshold.
        for (let i = 0; i < 60; i++) {
            idx.insert(`src/big/f${i}.ts`);
        }
        const expanded = idx.enumerateFolder('src/big');
        assert.ok(expanded !== null, 'a 60-file folder must NOT trip the give-up gate');
        assert.strictEqual(expanded!.length, 50, 'expansion must cap at FOLDER_EXPANSION_CAP');

        // Now blow past the give-up gate.
        const idx2 = new WorkspacePathIndex();
        for (let i = 0; i < 250; i++) {
            idx2.insert(`big/f${i}.ts`);
        }
        const exp2 = idx2.enumerateFolder('big');
        assert.strictEqual(exp2, null, 'a 250-file folder must bail out with null');
    });

    // ── Bonus: extractMentions wires file + folder + dedup correctly ────
    test('extractMentions expands @folder via the trie and dedupes', () => {
        const idx = new WorkspacePathIndex();
        idx.insert('src/a.ts');
        idx.insert('src/b.ts');
        idx.insert('lib/u.ts');

        const text = 'Refactor @file:lib/u.ts and clean up @folder:src — also @file:src/a.ts.';
        const mentions = extractMentions(text, idx);
        // Order: file mentions in source order, with folder expansion in between.
        // Dedup is applied across all sources.
        assert.ok(mentions.includes('lib/u.ts'),  'file mention should be present');
        assert.ok(mentions.includes('src/a.ts'),  'folder expansion + explicit file dedup');
        assert.ok(mentions.includes('src/b.ts'),  'folder expansion includes sibling');
        // No duplicates.
        assert.strictEqual(new Set(mentions).size, mentions.length, 'no duplicates expected');
    });

});
