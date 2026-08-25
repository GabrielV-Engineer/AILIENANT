/**
 * `useSessionPersistence`'s in-flight snapshot — the `setState`-backed
 * fallback that survives a mid-stream tab switch faster than the 400ms
 * debounced `PERSIST_TRANSCRIPT` can flush to the host (13.0.9).
 *
 * Regression guard for a live bug report: switching tabs once during a
 * coding turn dropped the plan checkmarks and the Planning Explanation card;
 * switching repeatedly eventually dropped the whole step list. Root cause:
 * `InflightSnapshot` (the only thing this exact race falls back to) carried
 * `content`/`thinking*` but never `checklist`/`companions`/`timeline` — a
 * fast-enough teardown restored the prose while silently losing the rest.
 *
 * Uses the same jsdom-staging harness proven in activeTaskRestored.test.ts.
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
        // Already exists as non-configurable (extension host) — leave it.
    }
};
_setGlobal('window', _dom.window);
_setGlobal('document', _dom.window.document);
_setGlobal('HTMLElement', _dom.window.HTMLElement);
_setGlobal('Node', _dom.window.Node);
_setGlobal('Event', _dom.window.Event);
_setGlobal('MessageEvent', _dom.window.MessageEvent);
_setGlobal('MouseEvent', _dom.window.MouseEvent);
_setGlobal('navigator', _dom.window.navigator);
_setGlobal('IS_REACT_ACT_ENVIRONMENT', true);

import * as assert from 'assert';
import * as React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { _setVsCodeApiForTesting, type VsCodeApi } from '../shared/vscodeApi';
import type { PlanWBSStep } from '../shared/config';

function makeStub(): VsCodeApi {
    let store: unknown;
    return {
        postMessage(): void { /* no-op */ },
        getState<T = unknown>(): T | undefined { return store as T | undefined; },
        setState<T>(s: T): void { store = s; },
    };
}

function mount(el: React.ReactElement): { container: HTMLDivElement; root: Root } {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => { root.render(el); });
    return { container, root };
}

function unmount(container: HTMLDivElement, root: Root): void {
    act(() => { root.unmount(); });
    container.remove();
}

const wait = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

suite('13.0.9 — useSessionPersistence in-flight snapshot carries checklist/companions/timeline', () => {
    let useChatStore: typeof import('../workspace/chatStore').useChatStore;
    let useWorkspaceStore: typeof import('../workspace/workspaceStore').useWorkspaceStore;
    let useSessionPersistence: typeof import('../workspace/hooks/useSessionPersistence').useSessionPersistence;

    suiteSetup(async () => {
        _setVsCodeApiForTesting(makeStub());
        ({ useChatStore } = await import('../workspace/chatStore.js'));
        ({ useWorkspaceStore } = await import('../workspace/workspaceStore.js'));
        ({ useSessionPersistence } = await import('../workspace/hooks/useSessionPersistence.js'));
    });

    suiteTeardown(() => {
        _setVsCodeApiForTesting(undefined);
    });

    function Harness(): null {
        useSessionPersistence();
        return null;
    }

    setup(() => {
        useWorkspaceStore.setState({ inflightTurn: null });
    });

    const tasks: PlanWBSStep[] = [
        { step_number: 1, target_role: 'core_dev', action: 'edit_file', target_file: 'a.py', description: 'bump', status: 'completed' },
    ];

    test('a mid-stream snapshot carries the checklist and companions, not just prose', async () => {
        useChatStore.setState({
            messages: [{
                id: 'turn-1', role: 'assistant', content: 'working on it',
                streaming: true, checklist: tasks,
                companions: [{
                    session_id: 's1', task_id: 't1', correlation_id: 't1:1', degraded: false,
                    objective: 'Bump the increment.', decisions: [], patterns_applied: [],
                    bottlenecks: [], errors_found: [], follow_ups: [], security_notes: [],
                }],
            }],
        });
        const { container, root } = mount(React.createElement(Harness));
        await act(async () => { await wait(250); }); // past the 200ms throttle

        const snap = useWorkspaceStore.getState().inflightTurn;
        assert.ok(snap, 'inflightTurn should be populated while a turn streams');
        assert.strictEqual(snap!.id, 'turn-1');
        assert.deepStrictEqual(snap!.checklist, tasks, 'checklist must survive into the fallback snapshot');
        assert.strictEqual(snap!.companions?.[0]?.objective, 'Bump the increment.');
        unmount(container, root);
    });

    test('restoring from the snapshot after a simulated teardown brings the checklist back', async () => {
        useChatStore.setState({
            messages: [{
                id: 'turn-2', role: 'assistant', content: 'working on it', streaming: true, checklist: tasks,
            }],
        });
        const { container: c1, root: r1 } = mount(React.createElement(Harness));
        await act(async () => { await wait(250); });
        unmount(c1, r1); // simulate the webview teardown (retainContextWhenHidden:false)

        // Fresh mount, empty transcript — exactly what a torn-down-and-recreated
        // webview looks like before REHYDRATE_TRANSCRIPT (if any) arrives.
        useChatStore.setState({ messages: [] });
        const { container: c2, root: r2 } = mount(React.createElement(Harness));

        const restored = useChatStore.getState().messages;
        assert.strictEqual(restored.length, 1);
        assert.strictEqual(restored[0].id, 'turn-2');
        assert.deepStrictEqual((restored[0] as { checklist?: PlanWBSStep[] }).checklist, tasks,
            'the plan checklist must survive the exact race this snapshot exists for');
        unmount(c2, r2);
    });
});
