// Contract test for the devcontainer interactive session bridge host driver.
//
// The handler is vscode-free — the CLI resolver, spawn, workspace root getter,
// sender, and env are all injected — so this runs as a pure contract test
// (mirrors devcontainerProvisioner.test.ts / devcontainerExecHandler.test.ts)
// with a fake child process the test drives deterministically.

import * as assert from 'assert';
import { EventEmitter } from 'events';
import type { ChildProcess } from 'child_process';
import {
    DevcontainerSessionHandler,
    DevcontainerSessionHandlerDeps,
} from '../providers/devcontainerSessionHandler';

const ROOT = '/work/project';

class FakeStream extends EventEmitter {
    pauseCalls = 0;
    resumeCalls = 0;
    pause(): this { this.pauseCalls += 1; return this; }
    resume(): this { this.resumeCalls += 1; return this; }
}

class FakeChild extends EventEmitter {
    stdout = new FakeStream();
    stderr = new FakeStream();
    stdinWrites: Buffer[] = [];
    stdin = { write: (data: Buffer) => { this.stdinWrites.push(data); return true; } };
    killed: string[] = [];
    kill(signal?: string): boolean {
        this.killed.push(signal ?? 'default');
        return true;
    }
}

interface SpawnCall { command: string; args: string[]; options: { cwd?: string; env?: NodeJS.ProcessEnv }; }

interface Harness {
    deps: DevcontainerSessionHandlerDeps;
    calls: SpawnCall[];
    children: FakeChild[];
    sent: Array<{ event_type: string; data: unknown }>;
}

function makeHarness(over: Partial<DevcontainerSessionHandlerDeps> = {}): Harness {
    const calls: SpawnCall[] = [];
    const children: FakeChild[] = [];
    const sent: Array<{ event_type: string; data: unknown }> = [];
    const deps: DevcontainerSessionHandlerDeps = {
        provisioner: { resolveCli: () => ({ command: 'devcontainer', baseArgs: [] }) },
        spawn: (command, args, options) => {
            calls.push({ command, args, options });
            const child = new FakeChild();
            children.push(child);
            return child as unknown as ChildProcess;
        },
        workspaceRoot: () => ROOT,
        send: (m) => sent.push(m),
        env: { CI: '1', SECRET: 'nope' },
        log: () => { /* silent */ },
        ...over,
    };
    return { deps, calls, children, sent };
}

suite('DevcontainerSessionHandler — open', () => {
    test('spawns `devcontainer exec --workspace-folder <root> -- /bin/sh` and reports ok:true', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: ['CI', 'MISSING'] },
        });
        assert.strictEqual(h.calls.length, 1);
        assert.deepStrictEqual(h.calls[0].args, [
            'exec', '--workspace-folder', ROOT, '--', '/bin/sh',
        ]);
        // Only whitelisted, present names are forwarded — value-by-name, no leakage.
        assert.deepStrictEqual(h.calls[0].options.env, { CI: '1' });
        assert.strictEqual(h.sent.length, 1);
        assert.strictEqual(h.sent[0].event_type, 'client_devcontainer_session_opened');
        assert.deepStrictEqual(h.sent[0].data, { session_id: 's', session_ref: 'r1', ok: true });
        assert.strictEqual(handler.sessionCount, 1);
    });

    test('no workspace folder open → ok:false, no spawn', () => {
        const h = makeHarness({ workspaceRoot: () => undefined });
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        assert.strictEqual(h.calls.length, 0);
        assert.strictEqual((h.sent[0].data as { ok: boolean }).ok, false);
    });

    test('spawn throwing → ok:false with the error detail, no crash', () => {
        const h = makeHarness({
            spawn: () => { throw new Error('ENOENT'); },
        });
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const data = h.sent[0].data as { ok: boolean; detail?: string };
        assert.strictEqual(data.ok, false);
        assert.match(data.detail ?? '', /ENOENT/);
    });

    test('re-opening a live session_ref does not spawn a second child', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        const open = {
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        };
        handler.handleServerEvent(open);
        handler.handleServerEvent(open);
        assert.strictEqual(h.calls.length, 1, 'a duplicate open must not spawn twice');
        assert.strictEqual(h.sent.length, 2);
        assert.strictEqual((h.sent[1].data as { ok: boolean }).ok, true);
    });
});

suite('DevcontainerSessionHandler — stdin / signal / flow', () => {
    function openOne(h: Harness, handler: DevcontainerSessionHandler): FakeChild {
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        return h.children[0];
    }

    test('stdin frame writes the decoded bytes to the child', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        const child = openOne(h, handler);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_stdin',
            data: { session_id: 's', session_ref: 'r1', data_b64: Buffer.from('echo hi\n').toString('base64') },
        });
        assert.strictEqual(child.stdinWrites.length, 1);
        assert.strictEqual(child.stdinWrites[0].toString('utf-8'), 'echo hi\n');
    });

    test('signal:interrupt sends SIGINT; signal:kill kills the child', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        const child = openOne(h, handler);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_signal',
            data: { session_id: 's', session_ref: 'r1', signal: 'interrupt' },
        });
        assert.deepStrictEqual(child.killed, ['SIGINT']);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_signal',
            data: { session_id: 's', session_ref: 'r1', signal: 'kill' },
        });
        assert.ok(child.killed.length >= 2, 'kill signal must request termination');
    });

    test('flow paused=true pauses both stdout and stderr; paused=false resumes both', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        const child = openOne(h, handler);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_flow',
            data: { session_id: 's', session_ref: 'r1', paused: true },
        });
        assert.strictEqual(child.stdout.pauseCalls, 1);
        assert.strictEqual(child.stderr.pauseCalls, 1);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_flow',
            data: { session_id: 's', session_ref: 'r1', paused: false },
        });
        assert.strictEqual(child.stdout.resumeCalls, 1);
        assert.strictEqual(child.stderr.resumeCalls, 1);
    });

    test('frames for an unknown session_ref are silently ignored', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        // No open() call first — nothing should throw.
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_stdin',
            data: { session_id: 's', session_ref: 'ghost', data_b64: '' },
        });
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_close',
            data: { session_id: 's', session_ref: 'ghost' },
        });
        assert.strictEqual(h.sent.length, 0);
    });
});

suite('DevcontainerSessionHandler — streaming + exit', () => {
    test('output is coalesced and the exit frame follows a flush, never precedes it', async () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const child = h.children[0];
        child.stdout.emit('data', Buffer.from('line1\n'));
        child.stderr.emit('data', Buffer.from('line2\n'));
        child.emit('close', 0);

        const streamFrames = h.sent.filter((s) => s.event_type === 'client_devcontainer_session_stream');
        const exitFrames = h.sent.filter((s) => s.event_type === 'client_devcontainer_session_exit');
        assert.strictEqual(exitFrames.length, 1);
        assert.strictEqual((exitFrames[0].data as { exit_code: number }).exit_code, 0);
        assert.strictEqual(streamFrames.length, 1, 'a synchronous burst must coalesce into one frame');
        const chunk = Buffer.from(
            (streamFrames[0].data as { chunk_b64: string }).chunk_b64, 'base64',
        ).toString('utf-8');
        assert.strictEqual(chunk, 'line1\nline2\n');
        // Ordering: the stream frame precedes the exit frame in send order.
        const streamIdx = h.sent.indexOf(streamFrames[0]);
        const exitIdx = h.sent.indexOf(exitFrames[0]);
        assert.ok(streamIdx < exitIdx);
    });

    test('a child error also emits a terminal exit frame (never hangs the bridge)', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const child = h.children[0];
        child.emit('error', new Error('spawn failed mid-flight'));
        const exitFrames = h.sent.filter((s) => s.event_type === 'client_devcontainer_session_exit');
        assert.strictEqual(exitFrames.length, 1);
        assert.strictEqual((exitFrames[0].data as { exit_code: number }).exit_code, -1);
    });

    test('close request kills the child, which then drives the exit frame', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const child = h.children[0];
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_close',
            data: { session_id: 's', session_ref: 'r1' },
        });
        assert.ok(child.killed.length > 0);
        assert.strictEqual(handler.sessionCount, 1, 'still tracked until the child actually exits');
        child.emit('close', 143);
        assert.strictEqual(handler.sessionCount, 0);
    });
});

suite('DevcontainerSessionHandler — dispose + idle ceiling', () => {
    test('dispose kills every tracked child', () => {
        const h = makeHarness();
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r2', cwd: ROOT, env_keys: [] },
        });
        assert.strictEqual(h.children.length, 2);
        handler.dispose();
        assert.ok(h.children[0].killed.length > 0);
        assert.ok(h.children[1].killed.length > 0);
        assert.strictEqual(handler.sessionCount, 0);
    });

    test('idle ceiling self-terminates a session with no stdin/flow activity', async () => {
        const h = makeHarness({ idleCeilingMs: 15 });
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const child = h.children[0];
        await new Promise((resolve) => setTimeout(resolve, 60));
        assert.ok(child.killed.length > 0, 'idle session was never terminated');
    });

    test('a stdin frame resets the idle ceiling', async () => {
        const h = makeHarness({ idleCeilingMs: 40 });
        const handler = new DevcontainerSessionHandler(h.deps);
        handler.handleServerEvent({
            event_type: 'server_devcontainer_session_open',
            data: { session_id: 's', session_ref: 'r1', cwd: ROOT, env_keys: [] },
        });
        const child = h.children[0];
        // Keep touching the idle timer faster than it can expire.
        for (let i = 0; i < 3; i++) {
            await new Promise((resolve) => setTimeout(resolve, 20));
            handler.handleServerEvent({
                event_type: 'server_devcontainer_session_stdin',
                data: { session_id: 's', session_ref: 'r1', data_b64: '' },
            });
        }
        assert.strictEqual(child.killed.length, 0, 'activity must postpone the idle ceiling');
    });
});
