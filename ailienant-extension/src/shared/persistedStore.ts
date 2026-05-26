/**
 * Phase 7.11.2 (ADR-706 §4.5c) — Zustand `persist`-style middleware backed by
 * VS Code's `acquireVsCodeApi().setState()/getState()` rather than localStorage.
 *
 * Why not `zustand/middleware/persist`?
 * The built-in middleware expects a `localStorage`-shape adapter (`getItem(k)`,
 * `setItem(k, v)`) and stringifies the whole bag. VS Code's WebView API is
 * fundamentally different: ONE state slot per WebView (no key→value map), and
 * the panel-lifetime survival semantic is what we actually want. A focused
 * ~50-line middleware tailored to that shape is clearer than wrestling the
 * built-in into the wrong contract.
 *
 * Persistence contract:
 *   - On store creation: read the host-state once via `getState()`. If a
 *     payload exists AND its `__v` matches `options.version`, merge it onto
 *     the defaults; otherwise discard it (safe upgrade path — plan W4).
 *   - On every store mutation: rAF-coalesce a `setState()` write so a burst of
 *     100 updates costs ONE frame, not 100 calls (plan-aligned with VS Code's
 *     render budget).
 *
 * The middleware exposes its persistence slot under `options.key`, so multiple
 * stores per WebView (e.g., workspace + a future sub-store) can coexist by
 * partitioning the persisted JSON object.
 */
import { create, StateCreator, UseBoundStore, StoreApi } from 'zustand';
import { vscodeApi, VsCodeApi } from './vscodeApi';

export interface PersistOptions<T> {
    /** Unique slot key. Bump the suffix (`.v2` …) to invalidate old payloads. */
    key: string;
    /** Selector — only fields returned here are written to the host. */
    pick: (state: T) => Partial<T>;
    /** Schema version. A mismatch on hydrate discards the old payload. */
    version?: number;
}

interface PersistedEnvelope {
    /** Per-key payload bag. */
    slots: Record<string, { __v: number; data: unknown }>;
}

function readEnvelope(api: VsCodeApi): PersistedEnvelope {
    const raw = api.getState<PersistedEnvelope>();
    if (raw && typeof raw === 'object' && raw.slots && typeof raw.slots === 'object') {
        return raw;
    }
    return { slots: {} };
}

function writeEnvelope(api: VsCodeApi, env: PersistedEnvelope): void {
    api.setState(env);
}

/**
 * Wraps a Zustand `StateCreator` so the resulting store hydrates from the host
 * on first access and writes back its persistable slice on every change.
 * Returns the bound store hook + a `clearPersisted()` helper for tests.
 */
export function createPersistedStore<T extends object>(
    creator: StateCreator<T>,
    options: PersistOptions<T>,
): UseBoundStore<StoreApi<T>> & { clearPersisted: () => void } {
    const version = options.version ?? 1;
    const api = vscodeApi();

    // Hydrate the initial state from the host BEFORE the first render.
    const env = readEnvelope(api);
    const slot = env.slots[options.key];
    const hydrated: Partial<T> | undefined =
        slot && slot.__v === version && slot.data && typeof slot.data === 'object'
            ? (slot.data as Partial<T>)
            : undefined;

    const store = create<T>((set, get, storeApi) => {
        const initial = creator(set, get, storeApi);
        return hydrated ? { ...initial, ...hydrated } : initial;
    });

    // rAF-coalesced write — one batched setState per frame, regardless of
    // how many store mutations land in between. requestAnimationFrame is
    // chosen so the write pairs naturally with the next render frame.
    let scheduled = false;
    const flush = (): void => {
        scheduled = false;
        const cur = store.getState();
        const slice = options.pick(cur);
        const next = readEnvelope(api);
        next.slots[options.key] = { __v: version, data: slice };
        writeEnvelope(api, next);
    };
    const schedule = (): void => {
        if (scheduled) { return; }
        scheduled = true;
        if (typeof requestAnimationFrame === 'function') {
            requestAnimationFrame(flush);
        } else {
            // jsdom / mocha test environment — fall back to a microtask so the
            // test can assert deterministically after `await Promise.resolve()`.
            Promise.resolve().then(flush);
        }
    };

    store.subscribe(schedule);

    const bound = store as UseBoundStore<StoreApi<T>> & { clearPersisted: () => void };
    bound.clearPersisted = (): void => {
        const next = readEnvelope(api);
        delete next.slots[options.key];
        writeEnvelope(api, next);
    };
    return bound;
}
