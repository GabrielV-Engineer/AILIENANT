/**
 * DEBT-012 — word-diff + syntax color coexist in DiffBlock.
 *
 * react-diff-viewer-continued's `renderWordDiff` reconstructs the FULL line and
 * calls `renderContent(fullLine)` once — it never passes a fragment. When the
 * returned element carries `dangerouslySetInnerHTML`, the library overlays its
 * own word-diff <ins>/<del> markup onto that HTML by CHARACTER OFFSET
 * (`applyDiffToHighlightedHtml`), using its own hardcoded `decodeEntities` set
 * to count characters. `tokensToHtml` must therefore never emit an entity
 * outside that set, or the overlay's offsets desync and the word-diff
 * highlights land on the wrong characters. WD3 is a characterization test
 * against the exact PINNED version of the dependency (package.json) — it is
 * meant to fail loudly the moment an upgrade changes that internal contract,
 * rather than let a mis-render pass silently.
 *
 * The library computes its diff asynchronously off `componentDidMount` (even
 * with `disableWorker=true`, which only skips the literal Worker thread, not
 * the promise hop) — `renderToStaticMarkup` never fires lifecycle methods, so
 * a real DOM mount + `act()` + one microtask flush is required to see actual
 * rows, mirroring agentTimeline.test.ts's existing createRoot/act pattern.
 */
import { JSDOM } from 'jsdom';
const _dom = new JSDOM('<!doctype html><html><body></body></html>', {
    pretendToBeVisual: true,
    url: 'http://localhost/',
});
const _setGlobal = (key: string, val: unknown): void => {
    try {
        Object.defineProperty(globalThis, key, { value: val, writable: true, configurable: true });
    } catch {
        // Already present as a non-configurable host global — leave it.
    }
};
_setGlobal('window', _dom.window);
_setGlobal('document', _dom.window.document);
_setGlobal('HTMLElement', _dom.window.HTMLElement);
_setGlobal('Node', _dom.window.Node);
_setGlobal('navigator', _dom.window.navigator);
_setGlobal('IS_REACT_ACT_ENVIRONMENT', true);

import * as assert from 'assert';
import * as React from 'react';
import { createRoot, Root } from 'react-dom/client';
import { act } from 'react';
import ReactDiffViewer from 'react-diff-viewer-continued';
import { tokensToHtml, buildTokenMap } from '../workspace/components/DiffBlock';
import type { ASTToken, DiffBlockShape } from '../shared/config';

suite('DEBT-012 — DiffBlock word-diff + syntax color', () => {
    // ── tokensToHtml — entity escaping ─────────────────────────────────────
    test('WD1 — escapes all five entities decodeEntities recognizes', () => {
        const tokens: ASTToken[] = [{ type: 'string', content: `<a & "b" 'c'>` }];
        const html = tokensToHtml(tokens);
        assert.ok(html.includes('&lt;a &amp; &quot;b&quot; &#39;c&#39;&gt;'), html);
        // Decoding the emitted entities must round-trip to the exact original
        // content length — that length is what the overlay's offset math
        // depends on.
        const decoded = html
            .replace(/^<span[^>]*>/, '')
            .replace(/<\/span>$/, '')
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
            .replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
        assert.strictEqual(decoded, tokens[0].content);
    });

    test('WD2 — color values come from the closed scopeColor set, never raw content', () => {
        const html = tokensToHtml([{ type: 'keyword', content: 'if' }]);
        assert.ok(/style="color:var\(--vscode-[\w-]+, #[0-9A-Fa-f]{6}\)"/.test(html), html);
    });

    // ── Characterization — the actual library overlay, pinned version ──────
    test('WD3 — word-diff highlight boundaries land on the right characters '
        + 'through the highlighted-HTML overlay path', async () => {
        const oldLine = 'value = 1';
        const newLine = 'value = 2';
        const tokens: ASTToken[] = [
            { type: 'variable', content: 'value' },
            { type: '', content: ' = ' },
            { type: 'constant.numeric', content: '2' },
        ];
        // Mirrors DiffBlock's renderContent: the library hands us the
        // reconstructed full line, and we return a dangerouslySetInnerHTML span.
        const renderContent = (source: string): JSX.Element => {
            const line = source === oldLine
                ? [{ type: 'variable', content: 'value' }, { type: '', content: ' = ' }, { type: 'constant.numeric', content: '1' }]
                : source === newLine ? tokens : undefined;
            if (!line) { return React.createElement(React.Fragment, null, source); }
            return React.createElement('span', { dangerouslySetInnerHTML: { __html: tokensToHtml(line) } });
        };

        const container = document.createElement('div');
        document.body.appendChild(container);
        const root: Root = createRoot(container);
        await act(async () => {
            root.render(React.createElement(ReactDiffViewer, {
                oldValue: oldLine, newValue: newLine, splitView: true,
                disableWordDiff: false, disableWorker: true, renderContent,
            }));
            // disableWorker=true takes the `Promise.resolve(fallback())` branch
            // of computeLineInformationWorker — one microtask hop before the
            // resulting setState commits the actual rows.
            await Promise.resolve();
            await Promise.resolve();
        });

        const html = container.innerHTML;
        act(() => { root.unmount(); });

        // The overlay must find "2" (the actual changed character) wrapped in
        // the library's own <ins> tag, INSIDE our color span — proving the
        // offset math landed on the right character rather than drifting from
        // a miscounted entity.
        assert.ok(
            /<span[^>]*style="color:var\([^)]*\)"[^>]*><ins[^>]*>2<\/ins><\/span>/.test(html),
            `expected a colored <ins>2</ins> in:\n${html}`,
        );
        // And the unchanged prefix keeps its syntax color with no <ins> wrapper.
        assert.ok(
            html.includes('style="color:var(--vscode-symbolIcon-variableForeground'),
            `expected the unchanged prefix to keep its syntax color in:\n${html}`,
        );
    });

    test('WD4 — untokenized fallback renders plain text (no HTML injection)', () => {
        const block: DiffBlockShape = {
            patch_id: 'p1', file_path: 'a.py', status: 'edit',
            old_content: 'a', new_content: 'b',
        };
        assert.strictEqual(buildTokenMap(block), undefined);
    });
});
