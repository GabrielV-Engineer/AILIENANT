/**
 * Phase 7.16 — Checkpoint Gate (host-delegated tokenization).
 *
 * The whole point of the phase is that syntax highlighting was added WITHOUT
 * growing the webview bundle: the grammar engine runs in the extension host and
 * the webview only paints precomputed scope spans. This gate asserts that
 * contract against the shipped artifacts and the render path:
 *
 *   - BUNDLE: the production webview bundle stays under the size ceiling and
 *     contains no grammar engine; the engine lives in the host bundle instead.
 *   - THEME:  scope→color resolves to VS Code CSS variables (not static hex) —
 *     the mechanism that makes a theme switch repaint with no re-tokenization.
 *   - CHAT:   a fenced code block with host tokens renders as scope-colored
 *     spans; without tokens it falls back to plain text. The block identity the
 *     renderer looks up matches the one the request path computes.
 *   - DIFF:   the per-line content→token map resolves a line to its scope spans.
 *
 * Host-agnostic: render assertions use react-dom/server (no live DOM), and a
 * jsdom seam is staged up-front so importing the diff library never trips on an
 * absent `document` during module load.
 */
import { JSDOM } from 'jsdom';
const _dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
const _setGlobal = (key: string, val: unknown): void => {
    try {
        Object.defineProperty(globalThis, key, { value: val, writable: true, configurable: true });
    } catch {
        // Already present as a non-configurable host global — leave it.
    }
};
_setGlobal('window', _dom.window);
_setGlobal('document', _dom.window.document);
_setGlobal('navigator', _dom.window.navigator);

import * as assert from 'assert';
import * as fs from 'fs';
import * as path from 'path';
import { execFileSync } from 'child_process';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MarkdownRenderer } from '../workspace/components/MarkdownRenderer';
import { buildTokenMap } from '../workspace/components/DiffBlock';
import { scopeColor } from '../workspace/utils/scopeColor';
import { extractCodeBlocks, hashCodeBlock } from '../workspace/utils/StreamingMarkdownParser';
import type { ASTToken, DiffBlockShape } from '../shared/config';

// out/test/<this>.js → out/ → extension root
const EXTENSION_ROOT = path.resolve(__dirname, '..', '..');
const WEBVIEW_BUNDLE = path.join(EXTENSION_ROOT, 'dist', 'workspace.js');
const HOST_BUNDLE = path.join(EXTENSION_ROOT, 'dist', 'extension.js');
const CEILING_BYTES = 550 * 1024;
const GRAMMAR_LEAK_MARKERS = ['@shikijs', 'createHighlighterCore', 'engine-javascript'];

suite('Phase 7.16 — Checkpoint Gate (host-delegated tokenization)', function () {
    this.timeout(180_000);

    // Build the PRODUCTION bundles so the ceiling rows measure the real shipped
    // artifact (the dev build is unminified and intentionally larger). esbuild's
    // own production guards run here too, so an over-ceiling/leak regression fails
    // fast in setup before the explicit assertions even run.
    suiteSetup(() => {
        execFileSync('node', ['esbuild.js', '--production'], { cwd: EXTENSION_ROOT, stdio: 'ignore' });
    });

    // ── BUNDLE — the entire point of the phase ────────────────────────────
    test('BC1 — production webview bundle stays under the size ceiling', () => {
        assert.ok(fs.existsSync(WEBVIEW_BUNDLE), 'dist/workspace.js missing');
        const bytes = fs.statSync(WEBVIEW_BUNDLE).size;
        assert.ok(
            bytes <= CEILING_BYTES,
            `workspace.js ${(bytes / 1024).toFixed(1)} KB exceeds ${CEILING_BYTES / 1024} KB ceiling`,
        );
    });

    test('BC2 — no grammar engine leaked into the webview bundle', () => {
        const src = fs.readFileSync(WEBVIEW_BUNDLE, 'utf8');
        for (const marker of GRAMMAR_LEAK_MARKERS) {
            assert.ok(!src.includes(marker), `grammar marker "${marker}" leaked into workspace.js`);
        }
    });

    test('BC3 — grammar engine is present in the host bundle', () => {
        assert.ok(fs.existsSync(HOST_BUNDLE), 'dist/extension.js missing');
        const src = fs.readFileSync(HOST_BUNDLE, 'utf8');
        // Grammar object-literal keys survive minification (esbuild keeps object
        // property names), so a registered grammar's scope data proves the engine
        // and grammars were bundled host-side rather than dropped.
        assert.ok(
            src.includes('source.python'),
            'host bundle is missing grammar data — engine not bundled host-side',
        );
    });

    // ── THEME — the theme-flip mechanism ──────────────────────────────────
    test('THEME1 — scopes resolve to VS Code CSS variables, not static colors', () => {
        const cases: Array<[string, string]> = [
            ['source.python keyword.control.flow.python', '--vscode-symbolIcon-keywordForeground'],
            ['source.ts string.quoted.double.ts', '--vscode-debugTokenExpression-string'],
            ['constant.numeric.decimal.python', '--vscode-debugTokenExpression-number'],
            ['comment.line.number-sign.python', '--vscode-descriptionForeground'],
            ['source.python entity.name.function.python', '--vscode-symbolIcon-functionForeground'],
            ['keyword.operator.assignment.ts', '--vscode-symbolIcon-operatorForeground'],
        ];
        for (const [scope, cssVar] of cases) {
            const color = scopeColor(scope);
            assert.ok(color.startsWith('var(--vscode-'), `"${scope}" → "${color}" is not a CSS var (no theme-flip)`);
            assert.ok(color.includes(cssVar), `"${scope}" → "${color}" missing expected ${cssVar}`);
        }
    });

    test('THEME2 — unknown/empty scope falls back to the editor foreground var', () => {
        for (const scope of ['', 'totally.unknown.scope']) {
            const color = scopeColor(scope);
            assert.ok(color.startsWith('var(--vscode-editor-foreground'), `"${scope}" → "${color}"`);
        }
    });

    // ── CHAT — highlighting visible end-to-end through the renderer ────────
    test('CHAT1 — block identity matches between extractor and renderer hash', () => {
        const [block] = extractCodeBlocks('```python\nprint("hi")\n```');
        assert.ok(block, 'fenced block not extracted');
        assert.strictEqual(block.lang, 'python');
        assert.strictEqual(block.hash, hashCodeBlock('python', 'print("hi")'));
    });

    test('CHAT2 — fenced block with host tokens renders scope-colored spans', () => {
        const content = '```python\nprint(1)\n```';
        const [block] = extractCodeBlocks(content);
        const codeTokens: Record<string, ASTToken[][]> = {
            [block.hash]: [[
                { type: 'source.python support.function.builtin.python', content: 'print' },
                { type: 'punctuation.section.python', content: '(' },
                { type: 'constant.numeric.python', content: '1' },
                { type: 'punctuation.section.python', content: ')' },
            ]],
        };
        const html = renderToStaticMarkup(
            React.createElement(MarkdownRenderer, { content, parserState: undefined, streaming: false, codeTokens }),
        );
        assert.ok(
            html.includes('var(--vscode-symbolIcon-functionForeground'),
            'function token not painted with its scope color',
        );
        assert.ok(html.includes('>print</span>'), 'token content not rendered in a span');
    });

    test('CHAT3 — without tokens the block falls back to plain text (no scope spans)', () => {
        const content = '```python\nprint(1)\n```';
        const html = renderToStaticMarkup(
            React.createElement(MarkdownRenderer, { content, parserState: undefined, streaming: false }),
        );
        assert.ok(html.includes('print(1)'), 'plain code text missing');
        assert.ok(!html.includes('--vscode-symbolIcon'), 'unexpected scope spans rendered without tokens');
    });

    // ── DIFF — per-line content→token mapping ─────────────────────────────
    test('DIFF1 — content→token map resolves each side line to its scope spans', () => {
        const block: DiffBlockShape = {
            patch_id: 'p1', file_path: 'a.py', status: 'edit',
            old_content: 'x = 1', new_content: 'x = 2',
            old_ast_lines: [[{ type: 'meta.python', content: 'x = 1' }]],
            new_ast_lines: [[{ type: 'meta.python', content: 'x = 2' }]],
        };
        const map = buildTokenMap(block);
        assert.ok(map, 'token map not built when ast lines present');
        assert.strictEqual(map!.get('x = 1')?.[0].content, 'x = 1');
        assert.strictEqual(map!.get('x = 2')?.[0].content, 'x = 2');
    });

    test('DIFF2 — no ast lines → undefined map (monospace fallback preserved)', () => {
        const block: DiffBlockShape = {
            patch_id: 'p2', file_path: 'a.py', status: 'edit',
            old_content: 'a', new_content: 'b',
        };
        assert.strictEqual(buildTokenMap(block), undefined);
    });
});
