// Contract test for the Help documents resolver.
//
// docsCatalog is vscode-free by design — the extension root, the configured URL,
// and the existence probe are all injected — so this runs as a pure contract test
// (mirrors devcontainerExecHandler.test.ts).

import * as assert from 'assert';
import { resolveDocEntries, BUNDLED_DOCS, DOCS_DIST_DIR } from '../providers/docsCatalog';

const ROOT = '/ext';
const join = (...parts: string[]): string => parts.join('/');

/** Existence probe that reports only the named basenames as present. */
function present(...basenames: string[]): (p: string) => boolean {
    const wanted = new Set(basenames.map(b => join(ROOT, DOCS_DIST_DIR, b)));
    return (p) => wanted.has(p);
}

suite('docsCatalog — bundled guides', () => {
    test('all guides copied → one entry each, no configuration needed', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: '',
            exists: present(...BUNDLED_DOCS.map(d => d.file)),
            join,
        });
        assert.strictEqual(entries.length, BUNDLED_DOCS.length);
        assert.ok(entries.every(e => e.relativePath !== undefined && e.url === undefined));
        // Order is the catalog's order, so the user manual is met first.
        assert.strictEqual(entries[0].relativePath, join(DOCS_DIST_DIR, BUNDLED_DOCS[0].file));
    });

    test('a guide missing from the build is omitted, not offered', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: '',
            exists: present('HowToUseIt.md'),
            join,
        });
        assert.deepStrictEqual(
            entries.map(e => e.relativePath),
            [join(DOCS_DIST_DIR, 'HowToUseIt.md')],
        );
    });

    test('copy step never ran and no URL set → empty list (caller shows a warning)', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: '',
            exists: () => false,
            join,
        });
        assert.deepStrictEqual(entries, []);
    });
});

suite('docsCatalog — configured online URL', () => {
    test('is an extra entry appended after the guides, never a precondition', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: 'https://example.test/docs',
            exists: present(...BUNDLED_DOCS.map(d => d.file)),
            join,
        });
        assert.strictEqual(entries.length, BUNDLED_DOCS.length + 1);
        const last = entries[entries.length - 1];
        assert.strictEqual(last.url, 'https://example.test/docs');
        assert.strictEqual(last.relativePath, undefined);
    });

    test('whitespace-only URL is treated as unset', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: '   ',
            exists: () => false,
            join,
        });
        assert.deepStrictEqual(entries, []);
    });

    test('a URL alone still yields a usable action when no guide shipped', () => {
        const entries = resolveDocEntries({
            extensionPath: ROOT,
            docsUrl: 'https://example.test/docs',
            exists: () => false,
            join,
        });
        assert.strictEqual(entries.length, 1);
        assert.strictEqual(entries[0].url, 'https://example.test/docs');
    });
});
