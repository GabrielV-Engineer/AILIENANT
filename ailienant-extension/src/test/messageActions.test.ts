/**
 * Phase 7.11.8 (ADR-706 §4.5g) — MessageActions component contract tests.
 *
 * The per-message ↪ Branch button drives the entire Time-Travel UX flow,
 * so its state machine deserves explicit coverage:
 *
 *   1. First click flips the button into "Confirm?" state; second click
 *      posts the BRANCH_FROM_CHECKPOINT message to the host.
 *   2. The 3-second idle timer reverts the Confirm state on its own.
 *   3. The ⏹ icon variant renders for abort-savepoint sources (Phase 7.11.3).
 *   4. The post callback fires with the exact payload shape the host's
 *      BRANCH_FROM_CHECKPOINT case expects (regression guard for the
 *      WS payload contract).
 *
 * vscode-test runs inside a real VS Code Electron host, so `window` and
 * `document` exist natively — we render the component into a detached DOM
 * node and inspect / drive it via plain DOM APIs (no extra deps).
 */
// jsdom MUST be installed onto globalThis BEFORE react-dom is loaded so the
// renderer binds to OUR window/document (not the absent extension-host
// globals). vscode-test runs in the extension host (Node + Electron main
// process) where `document` is undefined; we therefore stage a jsdom seam
// up-front. JSDOM is a devDependency (already added in Phase 7.11.6) and is
// externalised in the production esbuild bundle, so this never ships.
import { JSDOM } from 'jsdom';
const _dom = new JSDOM('<!doctype html><html><body></body></html>', {
    pretendToBeVisual: true,
    url: 'http://localhost/',
});
// Some host globals (e.g. `navigator`) are getter-only on Node and reject
// assignment. We define each one defensively via defineProperty so we tolerate
// either world (Electron vs pure Node) without crashing during module load.
const _setGlobal = (key: string, val: unknown): void => {
    try {
        Object.defineProperty(globalThis, key, {
            value: val, writable: true, configurable: true,
        });
    } catch {
        // Already exists as non-configurable (extension host) — leave it.
    }
};
_setGlobal('window', _dom.window);
_setGlobal('document', _dom.window.document);
_setGlobal('HTMLElement', _dom.window.HTMLElement);
_setGlobal('Node', _dom.window.Node);
_setGlobal('Event', _dom.window.Event);
_setGlobal('MouseEvent', _dom.window.MouseEvent);
_setGlobal('navigator', _dom.window.navigator);
// React 18 looks for this flag to enable act() concurrent-mode warnings; set
// it explicitly so the test runner doesn't surface noisy "act() is not
// configured" lines.
_setGlobal('IS_REACT_ACT_ENVIRONMENT', true);

import * as assert from 'assert';
import * as React from 'react';
import { createRoot, Root } from 'react-dom/client';
import { act } from 'react';
import { MessageActions } from '../workspace/components/MessageActions';

interface PostedMsg {
    type: 'BRANCH_FROM_CHECKPOINT';
    session_id: string;
    checkpoint_id: string;
    message_index: number;
}

function render(
    props: Partial<React.ComponentProps<typeof MessageActions>> = {},
): { container: HTMLDivElement; posted: PostedMsg[]; root: Root; rerender: () => void } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const posted: PostedMsg[] = [];
    const fullProps: React.ComponentProps<typeof MessageActions> = {
        checkpoint_id: 'cid-abc',
        session_id: 'sess-1',
        message_index: 2,
        is_abort_savepoint: false,
        post: (m) => { posted.push(m); },
        ...props,
    };
    const root = createRoot(container);
    act(() => {
        root.render(React.createElement(MessageActions, fullProps));
    });
    const rerender = (): void => {
        act(() => {
            root.render(React.createElement(MessageActions, fullProps));
        });
    };
    return { container, posted, root, rerender };
}

function clickBranchButton(container: HTMLDivElement): void {
    const btn = container.querySelector<HTMLButtonElement>('.ws-msg-action-branch');
    if (!btn) { throw new Error('branch button missing from rendered output'); }
    act(() => {
        btn.click();
    });
}

suite('Phase 7.11.8 — MessageActions (Time-Travel branch button)', function () {
    // React's first render warms a fair amount of devtools machinery; give
    // the cold-start test enough headroom on slow CI.
    this.timeout(20_000);

    test('idle render shows the ⟲ rewind icon + empty (icon-only) label', () => {
        const { container, root } = render();
        const btn = container.querySelector('.ws-msg-action-branch');
        assert.ok(btn, 'button missing');
        assert.strictEqual(btn!.getAttribute('data-confirming'), 'false');
        const icon = btn!.querySelector('.ws-msg-action-icon');
        assert.strictEqual(icon!.textContent, '⟲');
        // Idle is an icon-only circular control: the label is empty until the
        // confirm step reveals "Confirm?".
        const label = btn!.querySelector('.ws-msg-action-label');
        assert.strictEqual(label!.textContent, '');
        act(() => root.unmount());
        container.remove();
    });

    test('first click flips to Confirm?; second click posts BRANCH_FROM_CHECKPOINT', () => {
        const { container, posted, root } = render({
            checkpoint_id: 'cid-xyz', session_id: 'sess-2', message_index: 7,
        });
        // First click — no post, button flips to confirming.
        clickBranchButton(container);
        assert.strictEqual(posted.length, 0, 'first click must NOT post');
        const btn = container.querySelector('.ws-msg-action-branch')!;
        assert.strictEqual(btn.getAttribute('data-confirming'), 'true');
        assert.strictEqual(btn.querySelector('.ws-msg-action-label')!.textContent, 'Confirm?');

        // Second click — posts the BRANCH_FROM_CHECKPOINT message verbatim.
        clickBranchButton(container);
        assert.strictEqual(posted.length, 1, 'second click must post exactly once');
        assert.deepStrictEqual(posted[0], {
            type: 'BRANCH_FROM_CHECKPOINT',
            session_id: 'sess-2',
            checkpoint_id: 'cid-xyz',
            message_index: 7,
        });
        // After dispatch, the button reverts back to idle.
        assert.strictEqual(btn.getAttribute('data-confirming'), 'false');
        act(() => root.unmount());
        container.remove();
    });

    test('abort-savepoint variant renders the ⏹ icon + warn-accent styling', () => {
        const { container, root } = render({ is_abort_savepoint: true });
        const wrap = container.querySelector('.ws-msg-actions');
        assert.strictEqual(wrap!.getAttribute('data-abort-savepoint'), 'true');
        const icon = container.querySelector('.ws-msg-action-icon');
        assert.strictEqual(icon!.textContent, '⏹', 'expected ⏹ icon for abort savepoint');
        const btn = container.querySelector('.ws-msg-action-branch');
        assert.ok(
            (btn!.getAttribute('aria-label') ?? '').includes('aborted'),
            'aria-label should mention aborted state',
        );
        act(() => root.unmount());
        container.remove();
    });

    test('the dispatched payload carries the exact message_index passed in', () => {
        // Regression guard: the host slices the parent's persisted transcript
        // at this index to seed the branched session's UI history. Drifting
        // the index by even 1 misplaces the branch boundary.
        const { container, posted, root } = render({ message_index: 13 });
        clickBranchButton(container);
        clickBranchButton(container);
        assert.strictEqual(posted[0].message_index, 13);
        act(() => root.unmount());
        container.remove();
    });
});
