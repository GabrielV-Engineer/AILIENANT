/**
 * Phase 11.9 — Dashboard Checkpoint Gate (VS Code webview half).
 *
 * The four 11.9 invariants that live inside the workspace WebviewPanel, not the
 * browser-reachable dashboard SPA (that half is `e2e/dashboard.spec.ts`,
 * Playwright-driven). Mirrors the naming/structure of the two sibling
 * checkpoint-gate precedents (`phase7_16_checkpoint_gate.test.ts`,
 * `phase7_17_checkpoint_gate.test.ts`) and the jsdom-staging + hand-rolled
 * `createRoot` pattern already proven in `messageActions.test.ts` — no new
 * testing-library dependency (all four target components are simple,
 * presentational, and reachable via `querySelector`/`.click()`).
 *
 *   A — ActiveTaskHeader appears while streaming, collapses + dismisses once settled.
 *   B — ReasoningStream renders byte-identically for native vs. simulated (the
 *       `[Simulated]` tag was deliberately removed in commit 0948f35 — provenance
 *       is carried in `thinkingSource` state but intentionally never rendered).
 *   C — MESSAGE_COMPACTION_THRESHOLD is 40 (not the stale "60+"); SessionSummaryCard
 *       folds/expands correctly.
 *   D — A WS message sequence that never includes `server_hitl_approval_request`
 *       never sets `hitlPending` (so `HITLInterventionCard` cannot mount), while the
 *       diff still lands in the transcript; a positive control proves the assertion
 *       isn't vacuously true.
 */
// jsdom MUST be installed onto globalThis BEFORE react-dom is loaded — see
// messageActions.test.ts for the full rationale (vscode-test's extension host has
// no `window`/`document`).
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
import { ActiveTaskHeader } from '../workspace/components/ActiveTaskHeader';
import { ReasoningStream } from '../workspace/components/ReasoningStream';
import { SessionSummaryCard } from '../workspace/components/SessionSummaryCard';
import { newThinkingTurn } from '../workspace/utils/thinkingReducer';
import { MESSAGE_COMPACTION_THRESHOLD } from '../workspace/types';
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

suite('Phase 11.9 — Dashboard Checkpoint Gate (webview)', function () {

    // ── A ──────────────────────────────────────────────────────────────────

    suite('A — ActiveTaskHeader appears on submit, clears on completion', () => {
        // 13.1.9 — the loader glyph, live status text and elapsed clock moved to
        // AgentTimeline's own live loader row (one source of truth for "what's
        // happening now" instead of two surfaces kept in sync by convention).
        // This header now renders only the prompt + Stop/Dismiss.
        test('active: shows the prompt and a Cancel control', () => {
            const { container, root } = mount(
                React.createElement(ActiveTaskHeader, {
                    prompt: 'do X', isTurnActive: true,
                    onCancel: () => undefined, onDismiss: () => undefined,
                }),
            );
            const header = container.querySelector('.ws-active-task');
            assert.ok(header, 'header must render while the turn is active');
            assert.strictEqual(header?.getAttribute('data-done'), 'false');
            assert.strictEqual(container.querySelector('.ws-active-task-prompt')?.textContent, 'do X');
            assert.ok(container.querySelector('.ws-active-task-cancel'), 'cancel control must render');
            unmount(container, root);
        });

        test('settled: collapses to the frozen prompt and dismiss fires onDismiss', () => {
            let dismissed = false;
            const { container, root } = mount(
                React.createElement(ActiveTaskHeader, {
                    prompt: 'do X', isTurnActive: false,
                    onCancel: () => undefined, onDismiss: () => { dismissed = true; },
                }),
            );
            const header = container.querySelector('.ws-active-task');
            assert.strictEqual(header?.getAttribute('data-done'), 'true');
            assert.strictEqual(container.querySelector('.ws-active-task-prompt')?.textContent, 'do X');
            const dismissBtn = container.querySelector<HTMLButtonElement>(
                '.ws-active-task-btn[aria-label="Dismiss task summary"]',
            );
            assert.ok(dismissBtn, 'dismiss button must render once settled');
            act(() => { dismissBtn?.click(); });
            assert.strictEqual(dismissed, true, 'clicking dismiss must fire onDismiss');
            unmount(container, root);
        });
    });

    // ── B ──────────────────────────────────────────────────────────────────

    suite('B — ReasoningStream renders identically for native vs. simulated', () => {
        test('render is byte-identical regardless of source; no "simulated" literal', () => {
            const baseProps = {
                thinking: 'because X implies Y', tokens: 12, elapsedMs: 1200,
                open: true, streaming: false, onToggle: () => undefined,
            };
            const native = mount(React.createElement(ReasoningStream, { ...baseProps, source: 'native' as const }));
            const nativeHtml = native.container.innerHTML;
            unmount(native.container, native.root);

            const simulated = mount(React.createElement(ReasoningStream, { ...baseProps, source: 'simulated' as const }));
            const simulatedHtml = simulated.container.innerHTML;
            assert.strictEqual(
                simulatedHtml, nativeHtml,
                'rendered output must be identical regardless of provenance (0948f35)',
            );
            assert.ok(!/simulated/i.test(simulatedHtml), 'no "simulated" literal may ever render');
            unmount(simulated.container, simulated.root);
        });

        test('thinkingSource state still carries provenance even though the UI never shows it', () => {
            const turn = newThinkingTurn('reasoning…', 3, Date.now(), 'simulated');
            assert.strictEqual(turn.thinkingSource, 'simulated');
        });
    });

    // ── C ──────────────────────────────────────────────────────────────────

    suite('C — SessionSummaryCard + MESSAGE_COMPACTION_THRESHOLD', () => {
        test('threshold is 40 (regression guard for the corrected "40+" wording)', () => {
            assert.strictEqual(MESSAGE_COMPACTION_THRESHOLD, 40);
        });

        test('folded mode renders the fold header; expanding reveals the prose + reveal button', () => {
            let toggled = false;
            const props = {
                mode: 'folded' as const, hiddenCount: 41, summaryText: 'the prior turns did X',
                expanded: false, onToggle: () => { toggled = true; },
                onRevealOriginal: () => undefined, onHideOriginal: () => undefined,
            };
            const { container, root } = mount(React.createElement(SessionSummaryCard, props));
            const card = container.querySelector('.ws-session-summary');
            assert.strictEqual(card?.getAttribute('data-mode'), 'folded');
            assert.strictEqual(card?.getAttribute('data-expanded'), 'false');
            assert.ok(container.textContent?.includes('41 earlier messages compacted into a summary'));
            act(() => { container.querySelector<HTMLButtonElement>('.ws-session-summary-head')?.click(); });
            assert.strictEqual(toggled, true, 'clicking the head must fire onToggle');
            unmount(container, root);

            const expanded = mount(React.createElement(SessionSummaryCard, { ...props, expanded: true }));
            assert.ok(expanded.container.querySelector('#ws-session-summary-body'), 'body must render when expanded');
            assert.ok(expanded.container.textContent?.includes('the prior turns did X'));
            assert.ok(expanded.container.querySelector('.ws-session-summary-reveal'));
            unmount(expanded.container, expanded.root);
        });
    });

    // ── D ──────────────────────────────────────────────────────────────────

    suite('D — WS dispatch: state_compacted and auto-accept', () => {
        // `useWorkspaceStore`/`vscode_bridge` call the real `acquireVsCodeApi()` at
        // module load, which doesn't exist in the extension host — a stub MUST be
        // injected before `useWSMessageHandler` (which statically imports both) is
        // ever imported. Dynamic import, run once for the whole suite.
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
            useChatStore.setState({ messages: [], hitlPending: undefined });
            return mount(React.createElement(Harness));
        }

        function dispatch(type: string, payload: unknown): void {
            act(() => {
                window.dispatchEvent(new MessageEvent('message', { data: { type, payload } }));
            });
        }

        test('state_compacted produces a system message carrying the compaction summary', () => {
            const { container, root } = mountHarness();
            dispatch('state_compacted', { turns_compressed: 12, summary_text: 'summary text' });

            const messages = useChatStore.getState().messages;
            const chip = messages[messages.length - 1] as unknown as {
                role: string;
                compaction?: { summaryText: string; turnsCompressed: number };
            };
            assert.strictEqual(chip.role, 'system');
            assert.deepStrictEqual(chip.compaction, { summaryText: 'summary text', turnsCompressed: 12 });
            unmount(container, root);
        });

        test('auto-accept: RENDER_DIFF without server_hitl_approval_request never sets hitlPending', () => {
            const { container, root } = mountHarness();
            dispatch('RENDER_DIFF', {
                patch_id: 'auto-1',
                files: [{ file_path: 'a.py', old_content: '', new_content: 'x', status: 'edit' }],
            });

            assert.strictEqual(
                useChatStore.getState().hitlPending, undefined,
                'auto-accept must never set hitlPending (no server_hitl_approval_request round-trip)',
            );
            const messages = useChatStore.getState().messages;
            const diffMsg = messages.find(m =>
                (m as unknown as { diffBlocks?: Array<{ patch_id: string }> }).diffBlocks?.some(d => d.patch_id === 'auto-1'),
            );
            assert.ok(diffMsg, 'the auto-accepted diff must still land in the transcript');
            unmount(container, root);
        });

        test('positive control: server_hitl_approval_request DOES set hitlPending', () => {
            const { container, root } = mountHarness();
            dispatch('server_hitl_approval_request', {
                approval_id: 'manual-1',
                files: [{ file_path: 'b.py', old_content: '', new_content: 'y', status: 'edit', patch_id: 'manual-1' }],
            });
            assert.notStrictEqual(
                useChatStore.getState().hitlPending, undefined,
                'a real HITL request must set hitlPending — guards the auto-accept test against a vacuous negative',
            );
            unmount(container, root);
        });
    });
});
