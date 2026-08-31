/**
 * APIClient.abortTaskViaHttp — the HTTP fallback for Stop when the WebSocket
 * itself is down (client_abort_mesh cannot reach the backend in that exact
 * scenario). This is the code path that exists FOR "everything else is
 * broken," so it must never surface as an uncaught rejection or a thrown
 * error — a network failure here IS the answer (signalled: false), not a
 * caller-facing exception.
 *
 * Runs inside the real VS Code extension host (same @vscode/test-electron
 * pipeline as extension.test.ts) — the real `vscode` import below is unused
 * by the code under test, exactly like that file's own precedent, since
 * abortTaskViaHttp itself never calls a vscode.* API.
 */
import * as assert from 'assert';
import * as vscode from 'vscode';
import { APIClient } from '../api/api_client';

void vscode; // present, unused — see file header

suite('Fix 3 — APIClient.abortTaskViaHttp', () => {
    const originalFetch = globalThis.fetch;

    teardown(() => {
        globalThis.fetch = originalFetch;
    });

    test('a successful response reports signalled: true', async () => {
        globalThis.fetch = (async () =>
            new Response(JSON.stringify({ signalled: true }), { status: 200 })) as typeof fetch;

        const result = await APIClient.getInstance().abortTaskViaHttp('sess-ok');
        assert.deepStrictEqual(result, { signalled: true });
    });

    test('a non-OK HTTP response degrades to signalled: false, not a thrown error', async () => {
        globalThis.fetch = (async () =>
            new Response('Internal Server Error', { status: 500 })) as typeof fetch;

        const result = await APIClient.getInstance().abortTaskViaHttp('sess-500');
        assert.deepStrictEqual(result, { signalled: false });
    });

    test('a network failure degrades to signalled: false, never an uncaught rejection', async () => {
        globalThis.fetch = (async () => {
            throw new TypeError('fetch failed');
        }) as typeof fetch;

        const result = await APIClient.getInstance().abortTaskViaHttp('sess-network-down');
        assert.deepStrictEqual(result, { signalled: false });
    });

    test('a malformed (non-JSON) OK response degrades to signalled: false', async () => {
        globalThis.fetch = (async () =>
            new Response('not json', { status: 200 })) as typeof fetch;

        const result = await APIClient.getInstance().abortTaskViaHttp('sess-malformed');
        assert.deepStrictEqual(result, { signalled: false });
    });
});
