// Contract test for the devcontainer host-side execution handler.
//
// The handler is vscode-free — provisioner, sender, env, and the no-config hook
// are injected — so this runs as a pure contract test (mirrors
// devcontainerProvisioner.test.ts) with a fake provisioner and a send recorder.

import * as assert from 'assert';
import * as path from 'path';
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
        resolveContainerWorkspaceFolder: async () => undefined,
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
    test('resolves env_keys NAMES only; streams stdout/stderr via onChunk, then exit', async () => {
        const rec = recorder();
        let passedEnv: NodeJS.ProcessEnv = {};
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'pytest', cwd: ROOT, env_keys: ['CI', 'MISSING'] },
            },
            {
                provisioner: fakeProvisioner({
                    // A real provisioner streams via onChunk as data arrives; this fake
                    // mirrors that contract instead of returning the final result only.
                    exec: async (_root, _cmd, env, _timeout, _cwd, onChunk) => {
                        passedEnv = env ?? {};
                        onChunk?.('stdout', 'ok');
                        onChunk?.('stderr', 'warn');
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

    test('a synchronous burst of chunks coalesces into one frame per stream, not one per data event', async () => {
        const rec = recorder();
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'noisy', cwd: ROOT, env_keys: [] },
            },
            {
                provisioner: fakeProvisioner({
                    exec: async (_root, _cmd, _env, _timeout, _cwd, onChunk) => {
                        for (let i = 0; i < 10; i++) {
                            onChunk?.('stdout', `line ${i}\n`);
                        }
                        return { stdout: '', stderr: '', exitCode: 0 };
                    },
                }),
                workspaceRoot: ROOT, send: rec.send, env: {},
            },
        );
        const streamFrames = rec.sent.filter((s) => s.event_type === 'client_devcontainer_exec_stream');
        assert.strictEqual(streamFrames.length, 1, 'a synchronous burst must coalesce, not emit per data event');
        assert.strictEqual(
            streamFrames[0].data.chunk,
            Array.from({ length: 10 }, (_, i) => `line ${i}\n`).join(''),
        );
        // Ordering: every stream frame precedes the terminal exit frame.
        assert.strictEqual(rec.sent[rec.sent.length - 1].event_type, 'client_devcontainer_exec_exit');
    });

    test('a chunk at the byte cap flushes immediately rather than accumulating past it', async () => {
        const rec = recorder();
        const big = 'x'.repeat(8192); // == _STREAM_CHUNK_CAP_BYTES
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'big', cwd: ROOT, env_keys: [] },
            },
            {
                provisioner: fakeProvisioner({
                    exec: async (_root, _cmd, _env, _timeout, _cwd, onChunk) => {
                        onChunk?.('stdout', big);
                        onChunk?.('stdout', 'tail');
                        return { stdout: '', stderr: '', exitCode: 0 };
                    },
                }),
                workspaceRoot: ROOT, send: rec.send, env: {},
            },
        );
        const streamFrames = rec.sent.filter((s) => s.event_type === 'client_devcontainer_exec_stream');
        // The cap-triggered flush and the final flush() residue land as two frames.
        assert.strictEqual(streamFrames.length, 2);
        assert.strictEqual(streamFrames[0].data.chunk, big);
        assert.strictEqual(streamFrames[1].data.chunk, 'tail');
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

suite('devcontainer host handler — cwd translation (DEBT-085)', () => {
    const CONTAINER_ROOT = '/workspaces/project';

    async function execCwd(over: {
        cwd: string;
        containerRoot: string | undefined;
    }): Promise<string | undefined> {
        let capturedCwd: string | undefined;
        await handleDevcontainerServerEvent(
            {
                event_type: 'server_devcontainer_exec_request',
                data: { session_id: 's', request_id: 'r', command: 'x', cwd: over.cwd, env_keys: [] },
            },
            {
                provisioner: fakeProvisioner({
                    resolveContainerWorkspaceFolder: async () => over.containerRoot,
                    exec: async (_root, _cmd, _env, _timeout, containerCwd) => {
                        capturedCwd = containerCwd;
                        return { stdout: '', stderr: '', exitCode: 0 };
                    },
                }),
                workspaceRoot: ROOT, send: () => { /* noop */ }, env: {},
            },
        );
        return capturedCwd;
    }

    test('a sub-directory cwd maps onto the container root', async () => {
        const hostCwd = path.join(ROOT, 'src', 'api');
        const got = await execCwd({ cwd: hostCwd, containerRoot: CONTAINER_ROOT });
        assert.strictEqual(got, path.posix.join(CONTAINER_ROOT, 'src', 'api'));
    });

    test('a Windows-style relative segment normalizes to POSIX before joining', async () => {
        // path.join uses the host's own separator, so on win32 this reproduces the
        // real `src\api` shape path.relative would hand back.
        const hostCwd = ROOT + path.sep + ['src', 'api'].join(path.sep);
        const got = await execCwd({ cwd: hostCwd, containerRoot: CONTAINER_ROOT });
        assert.strictEqual(got, '/workspaces/project/src/api');
        assert.ok(!got?.includes('\\'), 'container path must never carry a host separator');
    });

    test('cwd equal to the workspace root ⇒ no prefix (runs at container root)', async () => {
        const got = await execCwd({ cwd: ROOT, containerRoot: CONTAINER_ROOT });
        assert.strictEqual(got, undefined);
    });

    test('empty cwd ⇒ no prefix', async () => {
        const got = await execCwd({ cwd: '', containerRoot: CONTAINER_ROOT });
        assert.strictEqual(got, undefined);
    });

    test('a cwd outside the workspace root refuses to translate', async () => {
        const outside = path.join(path.dirname(ROOT), 'other-project');
        const got = await execCwd({ cwd: outside, containerRoot: CONTAINER_ROOT });
        assert.strictEqual(got, undefined);
    });

    test('no resolvable container root ⇒ unprefixed, identical to pre-085 behavior', async () => {
        const hostCwd = path.join(ROOT, 'src');
        const got = await execCwd({ cwd: hostCwd, containerRoot: undefined });
        assert.strictEqual(got, undefined);
    });
});
