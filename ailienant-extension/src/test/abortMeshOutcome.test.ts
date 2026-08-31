/**
 * resolveAbortMeshOutcome — the ABORT_MESH routing decision extracted from
 * workspace_panel.ts's message-handler closure specifically so it's testable
 * without constructing a live WebviewPanel. Locks in the WS-connected vs.
 * WS-down branching (including 'reconnecting' being treated as down) that
 * this bug — a Stop click that both claimed success and reported failure at
 * once — lived in.
 */
import * as assert from 'assert';
import * as vscode from 'vscode';
import { resolveAbortMeshOutcome } from '../providers/workspace_panel';

void vscode; // present, unused — see apiClientAbort.test.ts's file header for why

suite('Fix 3 — resolveAbortMeshOutcome', () => {
    test('connected: sends over WS, never touches the HTTP fallback', async () => {
        const sent: unknown[] = [];
        const httpCalls: string[] = [];
        const outcome = await resolveAbortMeshOutcome('sess-connected', 'connected', {
            sendWs: (payload) => { sent.push(payload); },
            abortViaHttp: async (id) => { httpCalls.push(id); return { signalled: true }; },
        });

        assert.strictEqual(outcome.sentViaWs, true);
        assert.strictEqual(outcome.httpAck, undefined);
        assert.deepStrictEqual(sent, [
            { event_type: 'client_abort_mesh', data: { session_id: 'sess-connected' } },
        ]);
        assert.deepStrictEqual(httpCalls, []);
    });

    test('disconnected: falls back to HTTP, never sends over WS', async () => {
        const sent: unknown[] = [];
        const httpCalls: string[] = [];
        const outcome = await resolveAbortMeshOutcome('sess-down', 'disconnected', {
            sendWs: (payload) => { sent.push(payload); },
            abortViaHttp: async (id) => { httpCalls.push(id); return { signalled: false }; },
        });

        assert.strictEqual(outcome.sentViaWs, false);
        assert.deepStrictEqual(outcome.httpAck, { session_id: 'sess-down', signalled: false });
        assert.deepStrictEqual(httpCalls, ['sess-down']);
        assert.deepStrictEqual(sent, []);
    });

    test('reconnecting: treated the same as disconnected — falls back to HTTP', async () => {
        const sent: unknown[] = [];
        const outcome = await resolveAbortMeshOutcome('sess-reconnecting', 'reconnecting', {
            sendWs: (payload) => { sent.push(payload); },
            abortViaHttp: async () => ({ signalled: true }),
        });

        assert.strictEqual(outcome.sentViaWs, false);
        assert.deepStrictEqual(outcome.httpAck, { session_id: 'sess-reconnecting', signalled: true });
        assert.deepStrictEqual(sent, []);
    });

    test('the HTTP fallback\'s signalled value passes through unchanged, both ways', async () => {
        const okOutcome = await resolveAbortMeshOutcome('s1', 'disconnected', {
            sendWs: () => { /* noop */ },
            abortViaHttp: async () => ({ signalled: true }),
        });
        assert.strictEqual(okOutcome.httpAck?.signalled, true);

        const failOutcome = await resolveAbortMeshOutcome('s2', 'disconnected', {
            sendWs: () => { /* noop */ },
            abortViaHttp: async () => ({ signalled: false }),
        });
        assert.strictEqual(failOutcome.httpAck?.signalled, false);
    });
});
