/**
 * Phase 7.11.2 (ADR-706 §4.5c) — persistedStore middleware contract test.
 *
 * Verifies the rehydration round-trip without a real VS Code host:
 *   1. Mutations are flushed to the injected `vscodeApi` stub once per tick.
 *   2. Recreating the store with the same key restores the persisted slice.
 *   3. A version mismatch on hydrate discards the old payload (safe upgrade).
 *
 * Full UI behaviour (tab-switch survival, listener teardown) is covered by
 * the manual smoke per blueprint §6.2 — this test exists to keep the
 * middleware contract honest in CI.
 */
import * as assert from 'assert';
import { _setVsCodeApiForTesting, VsCodeApi } from '../shared/vscodeApi';
import { createPersistedStore } from '../shared/persistedStore';

interface Probe {
    count: number;
    name: string;
    setCount: (v: number) => void;
    setName: (v: string) => void;
}

function makeStub(): VsCodeApi & { _last: unknown } {
    let store: unknown = undefined;
    const stub = {
        postMessage(_msg: unknown): void { /* no-op */ },
        getState<T = unknown>(): T | undefined { return store as T | undefined; },
        setState<T>(state: T): void { store = state; (stub as { _last: unknown })._last = state; },
        _last: undefined as unknown,
    };
    return stub;
}

async function flushMicrotasks(): Promise<void> {
    // The middleware falls back to Promise.resolve().then(flush) when
    // requestAnimationFrame is absent (Node test env). Two microtask flushes
    // guarantee the rAF substitute has run.
    await Promise.resolve();
    await Promise.resolve();
}

suite('Phase 7.11.2 — persistedStore middleware', () => {
    test('rAF-coalesces mutations into a single setState write', async () => {
        const stub = makeStub();
        _setVsCodeApiForTesting(stub);

        const useStore = createPersistedStore<Probe>(
            (set) => ({
                count: 0,
                name: '',
                setCount: (v) => set({ count: v }),
                setName: (v) => set({ name: v }),
            }),
            { key: 'probe.v1', version: 1, pick: (s) => ({ count: s.count, name: s.name }) },
        );

        useStore.getState().setCount(5);
        useStore.getState().setCount(7);
        useStore.getState().setName('alpha');
        await flushMicrotasks();

        // The store wrote ONE envelope after the burst (last value wins).
        const envelope = stub.getState<{ slots: Record<string, { __v: number; data: Probe }> }>();
        assert.ok(envelope, 'envelope was never written');
        assert.deepStrictEqual(envelope.slots['probe.v1'], {
            __v: 1,
            data: { count: 7, name: 'alpha' },
        });

        _setVsCodeApiForTesting(undefined);
    });

    test('rehydrates initial state from the stub on second create', () => {
        const stub = makeStub();
        // Pre-seed an envelope as if the panel had been hidden+shown.
        stub.setState({ slots: { 'probe.v1': { __v: 1, data: { count: 42, name: 'beta' } } } });
        _setVsCodeApiForTesting(stub);

        const useStore = createPersistedStore<Probe>(
            (set) => ({
                count: 0,
                name: '',
                setCount: (v) => set({ count: v }),
                setName: (v) => set({ name: v }),
            }),
            { key: 'probe.v1', version: 1, pick: (s) => ({ count: s.count, name: s.name }) },
        );

        const s = useStore.getState();
        assert.strictEqual(s.count, 42, 'count was not rehydrated');
        assert.strictEqual(s.name, 'beta', 'name was not rehydrated');

        _setVsCodeApiForTesting(undefined);
    });

    test('version mismatch discards the old payload (safe upgrade path)', () => {
        const stub = makeStub();
        // Old payload stored at version 1 …
        stub.setState({ slots: { 'probe.v2': { __v: 1, data: { count: 999, name: 'legacy' } } } });
        _setVsCodeApiForTesting(stub);

        // … new store asks for version 2 — the legacy slot must be discarded.
        const useStore = createPersistedStore<Probe>(
            (set) => ({
                count: 0,
                name: '',
                setCount: (v) => set({ count: v }),
                setName: (v) => set({ name: v }),
            }),
            { key: 'probe.v2', version: 2, pick: (s) => ({ count: s.count, name: s.name }) },
        );

        const s = useStore.getState();
        assert.strictEqual(s.count, 0, 'legacy payload leaked across a version bump');
        assert.strictEqual(s.name, '', 'legacy payload leaked across a version bump');

        _setVsCodeApiForTesting(undefined);
    });
});
