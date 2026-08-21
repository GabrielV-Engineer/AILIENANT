/**
 * `ACTIVE_TASK_RESTORED` — host→webview restoration after a hidden panel is
 * torn down and reconstructed (13.0.8).
 *
 * `retainContextWhenHidden` is deliberately false (workspace_panel.ts), so
 * every hide→reveal cycle destroys the webview's JS context, wiping the
 * memory-only chatStore fields (`activeTaskPrompt`, `activeTaskStartedAt`,
 * `isTurnActive`) that drive the active-task header/spinner. The host's
 * `_runningTasks` map survives that cycle (host memory) and re-posts this
 * message on reveal when a task — including one merely paused on an
 * unanswered HITL card — is still in flight, so the header/spinner reappear
 * instead of silently vanishing (the reported bug: no way to tell whether a
 * task was cancelled or was still running after switching tabs).
 *
 * Uses the same jsdom-staging + `useWSMessageHandler` dispatch harness proven
 * in phase11_9_dashboard_checkpoint_gate.test.ts's section D.
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

suite('ACTIVE_TASK_RESTORED — active-task state survives a webview teardown', () => {
    let useChatStore: typeof import('../workspace/chatStore').useChatStore;
    let useWSMessageHandler: typeof import('../workspace/hooks/useWSMessageHandler').useWSMessageHandler;

    suiteSetup(async () => {
        _setVsCodeApiForTesting(makeStub());
        ({ useChatStore } = await import('../workspace/chatStore.js'));
        ({ useWSMessageHandler } = await import('../workspace/hooks/useWSMessageHandler.js'));
    });

    suiteTeardown(() => {
        _setVsCodeApiForTesting(undefined);
    });

    function Harness(): null {
        useWSMessageHandler();
        return null;
    }

    function mountHarness(): { container: HTMLDivElement; root: Root } {
        useChatStore.setState({
            activeTaskPrompt: undefined, activeTaskStartedAt: undefined, isTurnActive: false,
        });
        return mount(React.createElement(Harness));
    }

    function dispatch(type: string, extra: Record<string, unknown>): void {
        act(() => {
            window.dispatchEvent(new MessageEvent('message', { data: { type, ...extra } }));
        });
    }

    test('restores prompt/startedAt and re-arms isTurnActive', () => {
        const { container, root } = mountHarness();
        dispatch('ACTIVE_TASK_RESTORED', { prompt: 'build the landing page stack', startedAt: 1_700_000_000_000 });

        const s = useChatStore.getState();
        assert.strictEqual(s.activeTaskPrompt, 'build the landing page stack');
        assert.strictEqual(s.activeTaskStartedAt, 1_700_000_000_000);
        assert.strictEqual(s.isTurnActive, true);
        unmount(container, root);
    });

    test('a malformed payload (missing fields) is a no-op, not a crash or false-positive', () => {
        const { container, root } = mountHarness();
        dispatch('ACTIVE_TASK_RESTORED', {});

        const s = useChatStore.getState();
        assert.strictEqual(s.activeTaskPrompt, undefined);
        assert.strictEqual(s.activeTaskStartedAt, undefined);
        assert.strictEqual(s.isTurnActive, false);
        unmount(container, root);
    });
});
