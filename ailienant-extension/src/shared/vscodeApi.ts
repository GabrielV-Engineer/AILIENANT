/**
 * Phase 7.11.2 (ADR-706 §4.5c) — Typed singleton wrapper around VS Code's WebView API.
 *
 * `acquireVsCodeApi()` may only be called ONCE per WebView (calling it twice
 * throws). This module lazy-initializes the API on first access and re-exports
 * the same instance, ensuring the singleton invariant across every importer.
 *
 * The IIFE bundle layout (workspace + sidebar each their own bundle, per
 * esbuild.js) means each WebView gets its OWN module copy → its own
 * `acquireVsCodeApi()` call → which is correct (one call per WebView).
 *
 * Typed `getState<T>()` / `setState<T>()` map to VS Code's native API and back
 * the persisted-store middleware in `./persistedStore.ts`.
 */

export interface VsCodeApi {
    postMessage(message: unknown): void;
    getState<T = unknown>(): T | undefined;
    setState<T>(state: T): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

let _api: VsCodeApi | undefined;

export function vscodeApi(): VsCodeApi {
    if (!_api) {
        _api = acquireVsCodeApi();
    }
    return _api;
}

/**
 * Test-only escape hatch: replaces the cached singleton with a stub so unit
 * tests can verify the persistedStore middleware without a real VS Code host.
 * Production code MUST NOT call this — it is exposed solely for the mocha
 * suite under `src/test/`.
 */
export function _setVsCodeApiForTesting(stub: VsCodeApi | undefined): void {
    _api = stub;
}
