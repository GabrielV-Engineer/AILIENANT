/**
 * CoderCompanionCard (13.0.7) — the append-ordered, turn-scoped companion stack.
 *
 * Same up-front JSDOM seam as agentTimeline.test.ts so react-dom binds to our
 * window/document inside the vscode-test Electron host.
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
import { CoderCompanionCard } from '../workspace/components/CoderCompanionCard';
import type { CoderCompanionPayload } from '../api/contracts';

function payload(over: Partial<CoderCompanionPayload> & { correlation_id: string }): CoderCompanionPayload {
    return {
        session_id: 's1', task_id: 't1', objective: 'Explained something',
        decisions: [], patterns_applied: [], bottlenecks: [], security_notes: [],
        errors_found: [], follow_ups: [], degraded: false, scope: 'coding',
        ...over,
    };
}

function render(entries: CoderCompanionPayload[] | undefined, turnActive: boolean): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => { root.render(React.createElement(CoderCompanionCard, { entries, turnActive })); });
    return { container, root };
}

suite('13.0.7 — CoderCompanionCard', () => {

    test('nothing to show and the turn is not active ⇒ renders nothing', () => {
        const { container, root } = render(undefined, false);
        assert.strictEqual(container.querySelector('.ws-companion-stack'), null);
        act(() => root.unmount());
        container.remove();
    });

    test('turn active, nothing arrived yet ⇒ shows the pending skeleton', () => {
        const { container, root } = render(undefined, true);
        assert.ok(container.querySelector('.ws-companion-skeleton'), 'skeleton should show while the turn is active');
        act(() => root.unmount());
        container.remove();
    });

    test('several real entries each render their own card, in arrival order', () => {
        const entries = [
            payload({ correlation_id: 't1:ideation:0', objective: 'Round 1 explained' }),
            payload({ correlation_id: 't1:coding:0', objective: 'Patch set explained' }),
        ];
        const { container, root } = render(entries, false);
        const objectives = Array.from(container.querySelectorAll('.ws-companion-objective')).map(n => n.textContent);
        assert.deepStrictEqual(objectives, ['Round 1 explained', 'Patch set explained']);
        act(() => root.unmount());
        container.remove();
    });

    test('degraded entries collapse into one quiet count note, not a full card each', () => {
        const entries = [
            payload({ correlation_id: 't1:ideation:0', degraded: true }),
            payload({ correlation_id: 't1:ideation:1', degraded: true }),
        ];
        const { container, root } = render(entries, false);
        assert.strictEqual(container.querySelectorAll('.ws-companion-card').length, 0);
        assert.strictEqual(container.querySelector('.ws-companion-note')?.textContent, '2 explanations were unavailable.');
        act(() => root.unmount());
        container.remove();
    });

    test('a real entry AND the pending skeleton can show together mid-turn (more may still arrive)', () => {
        const entries = [payload({ correlation_id: 't1:ideation:0', objective: 'Round 1 explained' })];
        const { container, root } = render(entries, true);
        // The skeleton only shows when NOTHING has arrived yet (real.length === 0) —
        // once at least one real entry exists, further arrivals show up without a
        // preceding placeholder rather than a skeleton racing a real card.
        assert.strictEqual(container.querySelector('.ws-companion-skeleton'), null);
        assert.ok(container.querySelector('.ws-companion-objective'));
        act(() => root.unmount());
        container.remove();
    });

    test('scope label distinguishes an ideation entry from a coding one', () => {
        const entries = [
            payload({ correlation_id: 't1:ideation:0', scope: 'ideation' }),
            payload({ correlation_id: 't1:coding:0', scope: 'coding' }),
        ];
        const { container, root } = render(entries, false);
        const titles = Array.from(container.querySelectorAll('.ws-companion-title')).map(n => n.textContent);
        assert.ok(titles[0]?.includes('Clarification Explanation'));
        assert.ok(titles[1]?.includes('Coding Explanation'));
        act(() => root.unmount());
        container.remove();
    });
});
