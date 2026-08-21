/**
 * PlanAcceptancePanel — isTurnActive gating (13.0.7).
 *
 * Fixes the same stale `isStreaming`-only gating bug already closed for
 * ActiveTaskHeader/PromptBar in 13.0.6: `isStreaming` only covers token
 * delivery, so the three decision buttons (and the feedback input) were
 * clickable while the backend was still genuinely busy (node execution, tool
 * calls) between streams. `isTurnActive` covers the whole turn.
 *
 * Same up-front JSDOM seam as agentTimeline.test.ts.
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
import { PlanAcceptancePanel, type PlanAcceptancePanelProps } from '../workspace/components/PlanAcceptancePanel';
import type { PlanDocumentShape } from '../shared/config';

const PLAN: PlanDocumentShape = {
    summary: 'Ship the feature', outcome: 'Feature shipped', scope: [], decisions: [],
    constraints: [], tasks: [], checks: [], ubiquitous_language: {},
};

function render(over: Partial<PlanAcceptancePanelProps> = {}): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const props: PlanAcceptancePanelProps = {
        plan: PLAN,
        onAutoAccept: () => { /* noop */ },
        onManualApprove: () => { /* noop */ },
        onKeepPlanning: () => { /* noop */ },
        onClose: () => { /* noop */ },
        ...over,
    };
    const root = createRoot(container);
    act(() => { root.render(React.createElement(PlanAcceptancePanel, props)); });
    return { container, root };
}

suite('13.0.7 — PlanAcceptancePanel isTurnActive gating', () => {

    test('isTurnActive=true disables all three decision buttons and the feedback input', () => {
        const { container, root } = render({ isTurnActive: true });
        const buttons = container.querySelectorAll<HTMLButtonElement>('.plan-acceptance-buttons button');
        assert.strictEqual(buttons.length, 3);
        buttons.forEach(b => assert.strictEqual(b.disabled, true));
        assert.strictEqual(container.querySelector<HTMLInputElement>('.plan-feedback-input')?.disabled, true);
        act(() => root.unmount());
        container.remove();
    });

    test('isTurnActive=false (default) leaves every control enabled', () => {
        const { container, root } = render();
        const buttons = container.querySelectorAll<HTMLButtonElement>('.plan-acceptance-buttons button');
        buttons.forEach(b => assert.strictEqual(b.disabled, false));
        assert.strictEqual(container.querySelector<HTMLInputElement>('.plan-feedback-input')?.disabled, false);
        act(() => root.unmount());
        container.remove();
    });

    test('the close button is never disabled by isTurnActive and invokes onClose', () => {
        let closed = false;
        const { container, root } = render({ isTurnActive: true, onClose: () => { closed = true; } });
        const closeBtn = container.querySelector<HTMLButtonElement>('.plan-acceptance-close');
        assert.ok(closeBtn, 'expected a .plan-acceptance-close button');
        assert.strictEqual(closeBtn?.disabled, false);
        act(() => { closeBtn?.click(); });
        assert.strictEqual(closed, true);
        act(() => root.unmount());
        container.remove();
    });
});
