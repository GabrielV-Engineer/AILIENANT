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
// JSDOM has no layout engine, so ResizeObserver doesn't exist — needed by
// PtyPanel (useWindowedRows) once a cell row with non-empty PTY output mounts.
class _NoopResizeObserver {
    observe(): void { /* noop — no layout in JSDOM */ }
    unobserve(): void { /* noop */ }
    disconnect(): void { /* noop */ }
}
_setGlobal('ResizeObserver', _NoopResizeObserver);

import * as assert from 'assert';
import * as React from 'react';
import { createRoot, Root } from 'react-dom/client';
import { act } from 'react';
import { AgentTimeline, type AgentTimelineProps } from '../workspace/components/AgentTimeline';
import type { TimelineEntry, PlanWBSStep, DiffBlockShape, CellIterationShape, ExecutionDetailShape } from '../shared/config';

function render(props: Partial<AgentTimelineProps> & { entries: TimelineEntry[] }): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const full: AgentTimelineProps = {
        streaming: false,
        // Default to "this is the current turn" — the vast majority of these
        // tests exercise streaming/label/row behavior and don't care about
        // the newer-turn-collapses-this-one policy; the tests that DO care
        // set it explicitly.
        isLatestTurn: true,
        ...props,
    };
    const root = createRoot(container);
    act(() => { root.render(React.createElement(AgentTimeline, full)); });
    return { container, root };
}

function rerender(root: Root, props: Partial<AgentTimelineProps> & { entries: TimelineEntry[] }): void {
    const full: AgentTimelineProps = { streaming: false, isLatestTurn: true, ...props };
    act(() => { root.render(React.createElement(AgentTimeline, full)); });
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

    test('a checklist with no timeline entries still renders (survives an empty entries[] round-trip)', () => {
        // Regression guard: stripReasoningForPersist only strips 'reasoning'
        // entries, but a turn whose entries were reasoning-only to begin with (or
        // whose 'plan' marker landed on a different message via a WS-ordering
        // race) round-trips through persistence as entries:[] while `checklist`
        // survives — the component used to bail to null on entries.length===0
        // regardless, silently hiding a checklist that was still present.
        const tasks: PlanWBSStep[] = [
            { step_number: 1, target_role: 'core_dev', action: 'edit_file', target_file: 'a.py', description: 'bump', status: 'completed' },
        ];
        const { container, root } = render({ entries: [], checklist: tasks, streaming: true });
        assert.ok(container.querySelector('.ws-timeline'), 'the timeline shell should still mount');
        assert.ok(container.querySelector('.ws-checklist-head'), 'the checklist should render even with no timeline entries');
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

    test('a past (not-the-latest), done turn mounts already-collapsed (rows hidden until re-expanded)', () => {
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'x.py' })],
            streaming: false,
            isLatestTurn: false,
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

    test('the current turn stays open after its own stream settles — done alone never collapses it', () => {
        // The corrected policy (13.0.9): unlike the five widgets this component
        // replaced, settling its OWN stream must NOT auto-collapse the current
        // turn — only a NEWER turn starting does. Regression guard for the
        // exact bug users hit: switching tabs (which re-renders with the same
        // isLatestTurn=true) used to strip the plan checklist / timeline the
        // instant the backend's "done" arrived, even though this was still the
        // most recent turn on screen.
        const { container, root } = render({
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'x.py' })],
            streaming: true,
            isLatestTurn: true,
        });
        assert.ok(container.querySelector('.ws-timeline-rows'), 'rows visible while streaming');

        // The turn settles (streaming -> false) but remains the latest turn.
        rerender(root, {
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'x.py' })],
            streaming: false,
            isLatestTurn: true,
        });
        assert.strictEqual(container.querySelector('.ws-timeline')?.getAttribute('data-open'), 'true');
        assert.ok(container.querySelector('.ws-timeline-rows'), 'rows must stay visible after done while still latest');

        // A newer turn starts — NOW it collapses to the summary.
        rerender(root, {
            entries: [entry({ id: 'seq:0', kind: 'read', target: 'x.py' })],
            streaming: false,
            isLatestTurn: false,
        });
        assert.strictEqual(container.querySelector('.ws-timeline')?.getAttribute('data-open'), 'false');
        assert.strictEqual(container.querySelector('.ws-timeline-rows'), null);
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

    test('several reasoning entries in one turn each render their own block with independent toggles', () => {
        const { container, root } = render({
            entries: [
                entry({
                    id: 'r1', kind: 'reasoning', ref: 'r1', status: 'done', seq: 0,
                    thinking: 'Grounding the request…', thinkingElapsedMs: 1200,
                }),
                entry({
                    id: 'r2', kind: 'reasoning', ref: 'r2', status: 'active', seq: 1,
                    thinking: 'Composing the question batch…',
                }),
            ],
            streaming: true, // keep the container expanded so rows render
        });
        const rows = container.querySelectorAll('.ws-timeline-row[data-kind="reasoning"]');
        assert.strictEqual(rows.length, 2, 'both reasoning entries should render their own row');
        assert.ok(rows[0].textContent?.includes('Reasoned for'), 'r1 (frozen, collapsed) shows only its header label');
        assert.ok(rows[1].textContent?.includes('Composing the question batch'), 'r2 (open) shows its live body text');

        // The frozen entry (r1) defaults collapsed (thinkingOpen fallback, unset ⇒
        // false); the still-active entry (r2) defaults open.
        const bodies = container.querySelectorAll('.ws-reason-body');
        assert.strictEqual(bodies.length, 1, 'only the active (open) entry should show its body by default');
        assert.ok(bodies[0].textContent?.includes('Composing the question batch'));

        // Toggling r1's header opens ONLY r1 — independent per-entry state.
        const r1Header = rows[0].querySelector<HTMLButtonElement>('.ws-reason-header');
        act(() => { r1Header?.click(); });
        const bodiesAfter = container.querySelectorAll('.ws-reason-body');
        assert.strictEqual(bodiesAfter.length, 2, 'toggling r1 should open it without closing r2');
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
            isLatestTurn: false,
        });
        // Re-expand the outer timeline first (a past, not-the-latest turn mounts collapsed).
        act(() => { container.querySelector<HTMLButtonElement>('.ws-timeline-header')?.click(); });

        const rowHeader = container.querySelector<HTMLButtonElement>('.ws-timeline-row[data-kind="diff"] .ws-timeline-row-header');
        assert.ok(rowHeader, 'diff row header missing');
        assert.strictEqual(rowHeader!.getAttribute('aria-expanded'), 'false');
        assert.ok(rowHeader!.textContent?.includes('calc.py'));
        assert.ok(rowHeader!.textContent?.includes('+2 -1'));
        act(() => root.unmount());
        container.remove();
    });

    test('a SETTLED diff row defaults to OPEN on a done turn (auto-apply visibility)', () => {
        // A settled diff already landed on disk (RENDER_DIFF fires only after
        // PatchActuator.apply commits), so it is a confirmed record of what happened,
        // not a pending decision. In Auto mode the turn settles almost immediately —
        // under the plain "only the last row, only while streaming" default every
        // applied diff collapsed the instant the turn finished, so a multi-file
        // auto-applied turn showed no visible diff at all. Contrast with the
        // unsettled case directly above, which must still default collapsed.
        const diff: DiffBlockShape = {
            patch_id: 'p1', file_path: 'calc.py', old_content: 'a', new_content: 'b',
            status: 'edit', settled: true,
        };
        const { container, root } = render({
            entries: [entry({ id: 'calc.py', kind: 'diff', target: 'calc.py', metric: '+2 -1', diff })],
            streaming: false,
            isLatestTurn: false,
        });
        // Re-expand the outer timeline first (a past, not-the-latest turn mounts collapsed).
        act(() => { container.querySelector<HTMLButtonElement>('.ws-timeline-header')?.click(); });

        const rowHeader = container.querySelector<HTMLButtonElement>('.ws-timeline-row[data-kind="diff"] .ws-timeline-row-header');
        assert.ok(rowHeader, 'diff row header missing');
        assert.strictEqual(rowHeader!.getAttribute('aria-expanded'), 'true');
        act(() => root.unmount());
        container.remove();
    });

    test('command rows default to expanded — every one, not click-per-row', () => {
        // Regression guard: the old default only opened the LAST command row,
        // and only while the turn was still streaming — every other row (and
        // every row once the turn settled) needed an explicit click even
        // though it is the actual record of what ran, not a pending decision.
        const execA: ExecutionDetailShape = { source: 'native_host', exit_code: 0, stdout: 'ok' };
        const execB: ExecutionDetailShape = { source: 'native_host', exit_code: 1, stderr: 'boom' };
        const { container, root } = render({
            entries: [
                entry({ id: 'exec-1', kind: 'command', target: 'pytest -q', execution: execA }),
                entry({ id: 'exec-2', kind: 'command', target: 'mypy .', execution: execB }),
            ],
            streaming: false,
            isLatestTurn: true,
        });
        const headers = container.querySelectorAll<HTMLButtonElement>(
            '.ws-timeline-row[data-kind="command"] .ws-timeline-row-header',
        );
        assert.strictEqual(headers.length, 2);
        assert.strictEqual(headers[0].getAttribute('aria-expanded'), 'true', 'first command row should default open');
        assert.strictEqual(headers[1].getAttribute('aria-expanded'), 'true', 'second command row should default open too');
        act(() => root.unmount());
        container.remove();
    });

    test('a cell row renders CellAuditWidget for its single iteration (not the plain-label fallback)', () => {
        const cell: CellIterationShape = {
            iteration: 1,
            tools: [{ tool_name: 'run_terminal', args_scrubbed: { command: 'pytest' } }],
            pty: ['2 passed'],
            diffs: [],
        };
        const { container, root } = render({
            entries: [entry({ id: 'cell:1', kind: 'cell', target: 'run_terminal', metric: 'iteration 2', cell })],
            streaming: true,
        });
        const row = container.querySelector('.ws-timeline-row[data-kind="cell"]');
        assert.ok(row, 'cell row missing');
        const audit = row!.querySelector('.ws-cell-audit');
        assert.ok(audit, 'CellAuditWidget should be mounted for a cell row, not the generic label fallback');
        assert.strictEqual(row!.querySelector('.ws-cell-iter-num')?.textContent, '#2');
        assert.ok(row!.querySelector('.ws-cell-tool-name')?.textContent?.includes('run_terminal'));
        act(() => root.unmount());
        container.remove();
    });

    test('13.0.7: consecutive same-phase rows share one header; a phase change gets a new one, even repeating', () => {
        const entries: TimelineEntry[] = [
            entry({ id: 'a', kind: 'read', seq: 0, target: 'a.py' }),          // gather
            entry({ id: 'b', kind: 'retrieval', seq: 1 }),                     // gather (same run)
            entry({ id: 'c', kind: 'edit', seq: 2, target: 'a.py' }),          // act
            entry({ id: 'd', kind: 'retrieval', seq: 3 }),                     // gather — interrupts the act run
            entry({ id: 'e', kind: 'edit', seq: 4, target: 'b.py' }),          // act again — header repeats
        ];
        const { container, root } = render({ entries, streaming: true });
        const headers = Array.from(container.querySelectorAll('.ws-timeline-phase-label')).map(h => h.textContent);
        assert.deepStrictEqual(headers, [
            'Gathering context', 'Taking action', 'Gathering context', 'Taking action',
        ], 'one header per phase RUN, repeating when a phase is revisited — not deduped across the whole trace');
        act(() => root.unmount());
        container.remove();
    });

    test('13.0.7: a reasoning entry in the middle of an act run does not fragment it into two headers', () => {
        const entries: TimelineEntry[] = [
            entry({ id: 'a', kind: 'edit', seq: 0, target: 'a.py' }),          // act
            entry({ id: 'r1', kind: 'reasoning', ref: 'r1', seq: 1, status: 'active' }), // no phase
            entry({ id: 'b', kind: 'edit', seq: 2, target: 'b.py' }),          // still act — no new header
        ];
        const { container, root } = render({ entries, streaming: true });
        const headers = Array.from(container.querySelectorAll('.ws-timeline-phase-label')).map(h => h.textContent);
        assert.deepStrictEqual(headers, ['Taking action']);
        act(() => root.unmount());
        container.remove();
    });
});
