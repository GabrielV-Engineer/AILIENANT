/**
 * AgentTimeline (11.5.C.2) — the living-spine transcript component contract.
 *
 * Same up-front JSDOM seam as messageActions.test.ts / pipelineProgress.test.ts
 * so react-dom binds to our window/document inside the vscode-test Electron host.
 *
 * The diff row's DEFAULT (collapsed, done=true) state is tested; the row is
 * deliberately never clicked open here — DiffBlock mounts react-diff-viewer-
 * continued, a heavy external dependency with no existing precedent test in this
 * harness, so asserting its expanded internals is out of scope for this test.
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
import { AgentTimeline, type AgentTimelineProps } from '../workspace/components/AgentTimeline';
import type { TimelineEntry, PlanWBSStep, DiffBlockShape } from '../shared/config';

function render(props: Partial<AgentTimelineProps> & { entries: TimelineEntry[] }): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const full: AgentTimelineProps = {
        streaming: false,
        onReasoningToggle: () => { /* noop */ },
        ...props,
    };
    const root = createRoot(container);
    act(() => { root.render(React.createElement(AgentTimeline, full)); });
    return { container, root };
}

function entry(over: Partial<TimelineEntry> & { id: string; kind: TimelineEntry['kind'] }): TimelineEntry {
    return { seq: 0, ts: 100, status: 'done', ...over };
}

suite('11.5.C.2 — AgentTimeline', function () {
    this.timeout(20_000);

    test('renders nothing when there are no entries', () => {
        const { container, root } = render({ entries: [] });
        assert.strictEqual(container.querySelector('.ws-timeline'), null);
        act(() => root.unmount());
        container.remove();
    });

    test('header reads "Working…" while streaming', () => {
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'understanding', status: 'done' })],
            streaming: true,
        });
        assert.strictEqual(container.querySelector('.ws-timeline-label')?.textContent, 'Working…');
        act(() => root.unmount());
        container.remove();
    });

    test('done: header collapses to an honest summary — N actions, N files changed', () => {
        const entries: TimelineEntry[] = [
            entry({ id: 'seq:0', kind: 'understanding', seq: 0, ts: 100 }),
            entry({ id: 'a.py', kind: 'diff', seq: 1, ts: 105, target: 'a.py', ref: 'a.py' }),
            entry({ id: 'b.py', kind: 'diff', seq: 2, ts: 110, target: 'b.py', ref: 'b.py' }),
        ];
        const { container, root } = render({ entries, streaming: false });
        const label = container.querySelector('.ws-timeline-label')?.textContent ?? '';
        assert.ok(label.startsWith('Worked for'), `got: ${label}`);
        assert.ok(label.includes('3 actions'), `got: ${label}`);
        assert.ok(label.includes('2 files changed'), `got: ${label}`);
        act(() => root.unmount());
        container.remove();
    });

    test('done: the timeline auto-collapses (rows hidden until re-expanded)', () => {
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'x.py' })],
            streaming: false,
        });
        assert.strictEqual(container.querySelector('.ws-timeline-rows'), null);
        assert.strictEqual(container.querySelector('.ws-timeline')?.getAttribute('data-open'), 'false');

        // Re-expand via the header click.
        const header = container.querySelector<HTMLButtonElement>('.ws-timeline-header');
        act(() => { header?.click(); });
        assert.ok(container.querySelector('.ws-timeline-rows'), 'rows should reappear once re-expanded');
        act(() => root.unmount());
        container.remove();
    });

    test('a self-contained row (read) shows a friendly label with its target, no raw kind string', () => {
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'fibonacci.py' })],
            streaming: true, // stay expanded so rows render
        });
        const row = container.querySelector('.ws-timeline-row[data-kind="read"]');
        assert.ok(row, 'read row missing');
        assert.strictEqual(row!.textContent, 'Reading fibonacci.py');
        act(() => root.unmount());
        container.remove();
    });

    test('a reasoning row renders ReasoningStream with the live thinking text (when open)', () => {
        const { container, root } = render({
            entries: [entry({ id: 'r1', kind: 'reasoning', ref: 'r1', status: 'active' })],
            streaming: true,
            thinking: 'Considering the approach…',
            thinkingOpen: true,
        });
        const body = container.querySelector('.ws-reason-body');
        assert.ok(body, 'reasoning body should be mounted when thinkingOpen=true');
        assert.ok(body!.textContent?.includes('Considering the approach'));
        act(() => root.unmount());
        container.remove();
    });

    test('a plan row renders ExecutionChecklist ("Plan · N/M done")', () => {
        const tasks: PlanWBSStep[] = [
            { step_number: 1, target_role: 'core_dev', action: 'edit_file', target_file: 'a.py', description: 'bump', status: 'completed' },
            { step_number: 2, target_role: 'core_dev', action: 'edit_file', target_file: 'b.py', description: 'bump', status: 'pending' },
        ];
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'plan', metric: '2 steps' })],
            streaming: true,
            checklist: tasks,
        });
        const head = container.querySelector('.ws-checklist-head');
        assert.ok(head, 'ExecutionChecklist should be mounted for a plan row');
        assert.strictEqual(head!.textContent, 'Plan · 1/2 done');
        act(() => root.unmount());
        container.remove();
    });

    test('a diff row (done, not the most recent) defaults to collapsed, correct label', () => {
        const diff: DiffBlockShape = {
            patch_id: 'p1', file_path: 'calc.py', old_content: 'a', new_content: 'b', status: 'edit',
        };
        const { container, root } = render({
            entries: [entry({ id: 'calc.py', kind: 'diff', target: 'calc.py', metric: '+2 -1', diff })],
            streaming: false,
        });
        // Re-expand the outer timeline first (it auto-collapses on done).
        act(() => { container.querySelector<HTMLButtonElement>('.ws-timeline-header')?.click(); });

        const rowHeader = container.querySelector<HTMLButtonElement>('.ws-timeline-row[data-kind="diff"] .ws-timeline-row-header');
        assert.ok(rowHeader, 'diff row header missing');
        assert.strictEqual(rowHeader!.getAttribute('aria-expanded'), 'false');
        assert.ok(rowHeader!.textContent?.includes('calc.py'));
        assert.ok(rowHeader!.textContent?.includes('+2 -1'));
        act(() => root.unmount());
        container.remove();
    });
});
