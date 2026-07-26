/**
 * PipelineProgress — agent-activity status line contract.
 *
 * Guards the interim status relabel: raw internal node tokens (e.g. `context_gather`)
 * must never reach the screen, the coder's human free-text passes through, and the
 * done-state never asserts a step count (which would contradict the ExecutionChecklist)
 * — deferring to the checklist entirely when one exists.
 *
 * Uses the same up-front JSDOM seam as messageActions.test.ts so react-dom binds to
 * our window/document inside the vscode-test Electron host.
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
        // Already non-configurable (extension host) — leave it.
    }
};
_setGlobal('window', _dom.window);
_setGlobal('document', _dom.window.document);
_setGlobal('HTMLElement', _dom.window.HTMLElement);
_setGlobal('Node', _dom.window.Node);
_setGlobal('Event', _dom.window.Event);
_setGlobal('MouseEvent', _dom.window.MouseEvent);
_setGlobal('navigator', _dom.window.navigator);
_setGlobal('IS_REACT_ACT_ENVIRONMENT', true);

import * as assert from 'assert';
import * as React from 'react';
import { createRoot, Root } from 'react-dom/client';
import { act } from 'react';
import { PipelineProgress } from '../workspace/components/PipelineProgress';

function render(props: React.ComponentProps<typeof PipelineProgress>): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => { root.render(React.createElement(PipelineProgress, props)); });
    return { container, root };
}

function labelText(container: HTMLDivElement): string {
    return container.querySelector('.ws-thinking-label')?.textContent ?? '';
}

suite('PipelineProgress — activity status line', function () {
    this.timeout(20_000);

    test('a raw jargon node maps to a human phrase (never leaks the token)', () => {
        const { container, root } = render({ steps: ['context_gather'], done: false });
        const label = labelText(container);
        assert.ok(label.startsWith('Understanding your request'), `got: ${label}`);
        assert.ok(!label.includes('context_gather'), 'raw token leaked to the UI');
        act(() => root.unmount());
        container.remove();
    });

    test('human free-text passes through, capitalized', () => {
        const { container, root } = render({ steps: ['reading fibonacci.py'], done: false });
        assert.ok(labelText(container).startsWith('Reading fibonacci.py'));
        act(() => root.unmount());
        container.remove();
    });

    test('done-state shows no step count', () => {
        const { container, root } = render({ steps: ['context_gather', 'drafting_spec'], done: true });
        const label = labelText(container);
        assert.strictEqual(label, 'Task completed');
        assert.ok(!/\d/.test(label), 'a digit (step count) leaked into the done label');
        act(() => root.unmount());
        container.remove();
    });

    test('self-hides on completion when the turn carries a checklist', () => {
        const { container, root } = render({ steps: ['drafting_spec'], done: true, hasChecklist: true });
        assert.strictEqual(container.querySelector('.ws-thinking'), null, 'widget should defer to the checklist');
        act(() => root.unmount());
        container.remove();
    });
});
