/**
 * Phase 9 (ADR-707) — Native Thinking frontend contract tests.
 *
 *   1. The persisted `nativeThinking` toggle defaults ON and round-trips
 *      through the workspace store + the persist middleware (survives reload).
 *   2. The pure thinking reducers accumulate immutably, seed a new turn, and
 *      freeze the chronometric clock + collapse the box on the first answer
 *      token (idempotent).
 *
 * Full Thought Box render behaviour is covered by the manual smoke per plan §6
 * (the suite is Mocha+assert with stubs, not a DOM renderer).
 */
import * as assert from 'assert';
import { _setVsCodeApiForTesting, VsCodeApi } from '../shared/vscodeApi';
import { createPersistedStore } from '../shared/persistedStore';
import {
    accumulateThinking,
    newThinkingTurn,
    freezeThinkingOnText,
    ThinkingSlice,
} from '../workspace/utils/thinkingReducer';

// NOTE: `../workspace/workspaceStore` creates its persisted store at module
// load via `vscodeApi()`. The mocha suite runs in the extension host (no
// `acquireVsCodeApi`), so we MUST inject a stub *before* importing it — hence
// the dynamic import inside the test, never a top-level static import.

function makeStub(): VsCodeApi {
    let store: unknown = undefined;
    return {
        postMessage(_msg: unknown): void { /* no-op */ },
        getState<T = unknown>(): T | undefined { return store as T | undefined; },
        setState<T>(state: T): void { store = state; },
    };
}

async function flushMicrotasks(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

suite('Phase 9 (ADR-707) — Native Thinking toggle', () => {
    test('defaults ON and the setter flips it', async () => {
        // Inject a clean stub, THEN load the store so its module-load
        // vscodeApi() resolves to the stub (default state, nothing seeded).
        _setVsCodeApiForTesting(makeStub());
        const { useWorkspaceStore } = await import('../workspace/workspaceStore.js');
        assert.strictEqual(useWorkspaceStore.getState().nativeThinking, true);
        useWorkspaceStore.getState().setNativeThinking(false);
        assert.strictEqual(useWorkspaceStore.getState().nativeThinking, false);
        useWorkspaceStore.getState().setNativeThinking(true);
        assert.strictEqual(useWorkspaceStore.getState().nativeThinking, true);
        _setVsCodeApiForTesting(undefined);
    });

    test('persists through the workspace.v1 slot (survives reload)', async () => {
        const stub = makeStub();
        _setVsCodeApiForTesting(stub);

        // Mirror the workspaceStore persist contract for the toggle field.
        interface Slice { nativeThinking: boolean; set: (v: boolean) => void }
        const useMirror = createPersistedStore<Slice>(
            (set) => ({ nativeThinking: true, set: (v) => set({ nativeThinking: v }) }),
            { key: 'workspace.v1', version: 1, pick: (s) => ({ nativeThinking: s.nativeThinking }) },
        );

        useMirror.getState().set(false);
        await flushMicrotasks();

        const env = stub.getState<{ slots: Record<string, { __v: number; data: { nativeThinking: boolean } }> }>();
        assert.ok(env, 'envelope was never written');
        assert.strictEqual(env.slots['workspace.v1'].data.nativeThinking, false);

        _setVsCodeApiForTesting(undefined);
    });

    test('hydrates a persisted OFF value on recreate (opt-out is remembered)', () => {
        const stub = makeStub();
        stub.setState({ slots: { 'workspace.v1': { __v: 1, data: { nativeThinking: false } } } });
        _setVsCodeApiForTesting(stub);

        interface Slice { nativeThinking: boolean; set: (v: boolean) => void }
        const useMirror = createPersistedStore<Slice>(
            (set) => ({ nativeThinking: true, set: (v) => set({ nativeThinking: v }) }),
            { key: 'workspace.v1', version: 1, pick: (s) => ({ nativeThinking: s.nativeThinking }) },
        );

        assert.strictEqual(useMirror.getState().nativeThinking, false);
        _setVsCodeApiForTesting(undefined);
    });
});

suite('Phase 9 (ADR-707) — thinking reducers', () => {
    test('newThinkingTurn seeds an open, streaming assistant turn', () => {
        const t = newThinkingTurn('Let me ', 3, 1000);
        assert.strictEqual(t.role, 'assistant');
        assert.strictEqual(t.content, '');
        assert.strictEqual(t.streaming, true);
        assert.strictEqual(t.thinking, 'Let me ');
        assert.strictEqual(t.thinkingTokens, 3);
        assert.strictEqual(t.thinkingStartedAt, 1000);
        assert.strictEqual(t.thinkingOpen, true);
    });

    test('accumulateThinking appends immutably and keeps the start timestamp', () => {
        const turn: ThinkingSlice = { thinking: 'Let me ', thinkingTokens: 3, thinkingStartedAt: 1000, thinkingOpen: true };
        const next = accumulateThinking(turn, 'think', 6, 2000);
        assert.strictEqual(next.thinking, 'Let me think');
        assert.strictEqual(next.thinkingTokens, 6);
        // start is preserved, NOT overwritten by the later `now`.
        assert.strictEqual(next.thinkingStartedAt, 1000);
        // immutability — the source object is untouched.
        assert.strictEqual(turn.thinking, 'Let me ');
        assert.notStrictEqual(next, turn);
    });

    test('freezeThinkingOnText freezes elapsed + collapses on first answer token', () => {
        const turn: ThinkingSlice = { thinking: 'done reasoning', thinkingStartedAt: 1000 };
        const freeze = freezeThinkingOnText(turn, 3500);
        assert.deepStrictEqual(freeze, { thinkingElapsedMs: 2500, thinkingOpen: false });
    });

    test('freezeThinkingOnText is idempotent + null without thinking', () => {
        // Already frozen → no further update.
        const frozen: ThinkingSlice = { thinking: 'x', thinkingStartedAt: 1000, thinkingElapsedMs: 2500 };
        assert.strictEqual(freezeThinkingOnText(frozen, 9999), null);
        // No reasoning at all → nothing to freeze.
        assert.strictEqual(freezeThinkingOnText({}, 9999), null);
    });
});
