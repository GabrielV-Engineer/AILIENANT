// Contract test for the devcontainer host-side execution handler.
//
// The handler is vscode-free — provisioner, sender, env, and the no-config hook
// are injected — so this runs as a pure contract test (mirrors
// devcontainerProvisioner.test.ts) with a fake provisioner and a send recorder.

import * as assert from 'assert';
import { handleDevcontainerServerEvent, ProvisionerLike } from '../providers/devcontainerExecHandler';
import type { DevcontainerStatus, ExecResult } from '../providers/devcontainerProvisioner';

interface Sent { event_type: string; data: Record<string, unknown>; }

function recorder(): { send: (m: { event_type: string; data: unknown }) => void; sent: Sent[] } {
    const sent: Sent[] = [];
    return {
        send: (m) => sent.push({ event_type: m.event_type, data: m.data as Record<string, unknown> }),
        sent,
    };
}

function fakeProvisioner(over: Partial<ProvisionerLike>): ProvisionerLike {
    return {
        up: async (): Promise<DevcontainerStatus> => ({ state: 'ready', cliSource: 'path' }),
        exec: async (): Promise<ExecResult> => ({ stdout: '', stderr: '', exitCode: 0 }),
        ...over,
    };
}

const ROOT = '/work/project';

suite('devcontainer host handler — provisioning', () => {
    test('ready → emits provision_status ready', async () => {
        const rec = recorder();
        const handled = await handleDevcontainerServerEvent(
            { event_type: 'server_devcontainer_provision_request', data: { session_id: 's', request_id: 'r', cwd: ROOT } },
            { provisioner: fakeProvisioner({}), workspaceRoot: ROOT, send: rec.send, env: {} },
        );
        assert.strictEqual(handled, true);
        assert.strictEqual(rec.sent[0].event_type, 'client_devcontainer_provision_status');
        assert.strictEqual(rec.sent[0].data.state, 'ready');
    });

    test('missing devcontainer.json → failed + onNoDevcontainer fires', async () => {
        const rec = recorder();
        let prompted = false;
        await handleDevcontainerServerEvent(
            { event_type: 'server_devcontainer_provision_request', data: { session_id: 's', request_id: 'r', cwd: ROOT } },
            {
                provisioner: fakeProvisioner({
                    up: async () => ({ state: 'degraded', cliSource: 'path', detail: 'no devcontainer.json in workspace' }),
                }),
                workspaceRoot: ROOT, send: rec.send, env: {},
                onNoDevcontainer: () => { prompted = true; },
            },
        );
        assert.strictEqual(rec.sent[0].data.state, 'failed');
        assert.ok(prompted, 'onNoDevcontainer was not fired');
    });

    test('no workspace folder → failed', async () => {
        const rec = recorder();
        await handleDevcontainerServerEvent(
            { event_type: 'server_devcontainer_provision_request', data: { session_id: 's', request_id: 'r', cwd: ROOT } },
            { provisioner: fakeProvisioner({}), workspaceRoot: undefined, send: rec.send, env: {} },
        );
        assert.strictEqual(rec.sent[0].data.state, 'failed');
    });
});

suite('devcontainer host handler — exec', () => {
    test('resolves env_keys NAMES only and streams stdout/stderr + exit', async () => {
        const rec = recorder();
        let passedEnv: NodeJS.ProcessEnv = {};
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'pytest', cwd: ROOT, env_keys: ['CI', 'MISSING'] },
            },
            {
                provisioner: fakeProvisioner({
                    exec: async (_root, _cmd, env) => {
                        passedEnv = env ?? {};
                        return { stdout: 'ok', stderr: 'warn', exitCode: 0 };
                    },
                }),
                workspaceRoot: ROOT, send: rec.send, env: { CI: '1', SECRET: 'nope' },
            },
        );
        // Only whitelisted, present names are forwarded — value-by-name, no leakage.
        assert.deepStrictEqual(passedEnv, { CI: '1' });
        const types = rec.sent.map((s) => s.event_type);
        assert.deepStrictEqual(types, [
            'client_devcontainer_exec_stream',
            'client_devcontainer_exec_stream',
            'client_devcontainer_exec_exit',
        ]);
        assert.strictEqual(rec.sent[2].data.exit_code, 0);
    });

    test('exec throws → emits exit -1 (never hangs the bridge)', async () => {
        const rec = recorder();
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'x', cwd: ROOT, env_keys: [] },
            },
            {
                provisioner: fakeProvisioner({ exec: async () => { throw new Error('boom'); } }),
                workspaceRoot: ROOT, send: rec.send, env: {},
            },
        );
        assert.strictEqual(rec.sent[0].event_type, 'client_devcontainer_exec_exit');
        assert.strictEqual(rec.sent[0].data.exit_code, -1);
    });

    test('non-devcontainer event is not handled', async () => {
        const rec = recorder();
        const handled = await handleDevcontainerServerEvent(
            { event_type: 'server_token_chunk', data: {} },
            { provisioner: fakeProvisioner({}), workspaceRoot: ROOT, send: rec.send, env: {} },
        );
        assert.strictEqual(handled, false);
        assert.strictEqual(rec.sent.length, 0);
    });
});
