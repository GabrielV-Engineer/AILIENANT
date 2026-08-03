// Contract test for the devcontainer provisioning driver.
//
// The driver core imports no `vscode`; every host capability is injected, so this
// runs as a pure contract test (mirrors persistedStore.test.ts) — fake `spawn`
// returns an EventEmitter-backed child the test drives deterministically.

import * as assert from 'assert';
import { EventEmitter } from 'events';
import type { ChildProcess } from 'child_process';
import {
    DevcontainerProvisioner,
    ProvisionerDeps,
    DEVCONTAINERS_EXTENSION_ID,
} from '../providers/devcontainerProvisioner';

const ROOT = '/work/project';

class FakeChild extends EventEmitter {
    stdout = new EventEmitter();
    stderr = new EventEmitter();
    killed: string[] = [];
    kill(signal?: string): boolean {
        this.killed.push(signal ?? 'default');
        return true;
    }
}

interface SpawnCall { command: string; args: string[]; }

interface Harness {
    deps: ProvisionerDeps;
    calls: SpawnCall[];
    children: FakeChild[];
}

/** Build injected deps. `autoClose` controls whether spawned children exit 0 on the next tick. */
function makeHarness(over: Partial<ProvisionerDeps> = {}, autoClose = true): Harness {
    const calls: SpawnCall[] = [];
    const children: FakeChild[] = [];
    const deps: ProvisionerDeps = {
        spawn: (command, args) => {
            const child = new FakeChild();
            calls.push({ command, args });
            children.push(child);
            if (autoClose) {
                setTimeout(() => child.emit('close', 0), 0);
            }
            return child as unknown as ChildProcess;
        },
        isExtensionInstalled: () => false,
        fileExists: () => true,
        log: () => { /* silent */ },
        bundledCliEntry: null,
        ...over,
    };
    return { deps, calls, children };
}

suite('DevcontainerProvisioner — probe', () => {
    test('resolveCli precedence: extension > bundled-dep > path', () => {
        const ext = new DevcontainerProvisioner(
            makeHarness({ isExtensionInstalled: (id) => id === DEVCONTAINERS_EXTENSION_ID }).deps,
        );
        assert.strictEqual(ext.resolveCli().source, 'devcontainers-extension');

        const bundled = new DevcontainerProvisioner(
            makeHarness({ isExtensionInstalled: () => false, bundledCliEntry: '/x/devcontainer.js' }).deps,
        );
        assert.strictEqual(bundled.resolveCli().source, 'bundled-dep');
        assert.deepStrictEqual(bundled.resolveCli().baseArgs, ['/x/devcontainer.js']);

        const pathOnly = new DevcontainerProvisioner(
            makeHarness({ isExtensionInstalled: () => false, bundledCliEntry: null }).deps,
        );
        assert.strictEqual(pathOnly.resolveCli().source, 'path');
        assert.strictEqual(pathOnly.resolveCli().command, 'devcontainer');
    });

    test('hasDevcontainerConfig reflects the injected filesystem', () => {
        const present = new DevcontainerProvisioner(makeHarness({ fileExists: () => true }).deps);
        assert.strictEqual(present.hasDevcontainerConfig(ROOT), true);

        const absent = new DevcontainerProvisioner(makeHarness({ fileExists: () => false }).deps);
        assert.strictEqual(absent.hasDevcontainerConfig(ROOT), false);
    });
});

suite('DevcontainerProvisioner — up()', () => {
    test('reaches ready on a clean provision', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        const status = await prov.up(ROOT);
        assert.strictEqual(status.state, 'ready');
        assert.strictEqual(h.calls.length, 1);
        assert.deepStrictEqual(h.calls[0].args, ['up', '--workspace-folder', ROOT]);
    });

    test('degrades cleanly when no devcontainer.json is present', async () => {
        const h = makeHarness({ fileExists: () => false });
        const prov = new DevcontainerProvisioner(h.deps);
        const status = await prov.up(ROOT);
        assert.strictEqual(status.state, 'degraded');
        assert.match(status.detail ?? '', /devcontainer\.json/);
        assert.strictEqual(h.calls.length, 0); // never spawned
    });

    test('single-flight: concurrent up() calls share one spawn', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        const [a, b] = await Promise.all([prov.up(ROOT), prov.up(ROOT)]);
        assert.strictEqual(a.state, 'ready');
        assert.strictEqual(b.state, 'ready');
        assert.strictEqual(h.calls.length, 1);
    });

    test('idempotent: up() after ready is a cached no-op', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);
        await prov.up(ROOT);
        assert.strictEqual(h.calls.length, 1);
    });

    test('timeout kills the child and degrades without hanging', async () => {
        const h = makeHarness({ provisionTimeoutMs: 20 }, /* autoClose */ false);
        const prov = new DevcontainerProvisioner(h.deps);
        const status = await prov.up(ROOT);
        assert.strictEqual(status.state, 'degraded');
        assert.match(status.detail ?? '', /timed out|failed/);
        assert.ok(h.children[0].killed.length > 0, 'child was not killed on timeout');
    });

    test('non-zero exit degrades', async () => {
        const h = makeHarness({}, /* autoClose */ false);
        const prov = new DevcontainerProvisioner(h.deps);
        const pending = prov.up(ROOT);
        // Drive a failing exit once the listener is attached.
        setTimeout(() => h.children[0].emit('close', 1), 0);
        const status = await pending;
        assert.strictEqual(status.state, 'degraded');
        assert.match(status.detail ?? '', /exited 1/);
    });
});

suite('DevcontainerProvisioner — exec() & status', () => {
    test('exec builds an argv-array command run through the container shell (no host shell)', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        const result = await prov.exec(ROOT, 'pytest -q');
        assert.strictEqual(result.exitCode, 0);
        assert.deepStrictEqual(h.calls[0].args, [
            'exec', '--workspace-folder', ROOT, '--', '/bin/sh', '-c', 'pytest -q',
        ]);
    });

    test('onDidChangeStatus fires on transitions and unsubscribes', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        const seen: string[] = [];
        const off = prov.onDidChangeStatus((s) => seen.push(s.state));
        await prov.up(ROOT);
        off();
        assert.ok(seen.includes('provisioning'), 'never saw provisioning');
        assert.ok(seen.includes('ready'), 'never saw ready');
    });

    test('exec() with a containerCwd prefixes cd into the shell command', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.exec(ROOT, 'pytest -q', undefined, undefined, "/remote/it's mine");
        assert.deepStrictEqual(h.calls[0].args, [
            'exec', '--workspace-folder', ROOT, '--', '/bin/sh', '-c',
            "cd '/remote/it'\\''s mine' && pytest -q",
        ]);
    });

    test('exec() without a containerCwd is unprefixed (byte-identical to pre-085)', async () => {
        const h = makeHarness();
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.exec(ROOT, 'pytest -q');
        assert.deepStrictEqual(h.calls[0].args, [
            'exec', '--workspace-folder', ROOT, '--', '/bin/sh', '-c', 'pytest -q',
        ]);
    });

    test('exec() forwards each data event to onChunk while still accumulating the full result (DEBT-083)', async () => {
        const h = makeHarness({}, /* autoClose */ false);
        const prov = new DevcontainerProvisioner(h.deps);
        const seen: Array<{ stream: string; text: string }> = [];
        const pending = prov.exec(ROOT, 'noisy-command', undefined, undefined, undefined, (stream, text) => {
            seen.push({ stream, text });
        });
        const child = h.children[0];
        child.stdout.emit('data', Buffer.from('a'));
        child.stdout.emit('data', Buffer.from('b'));
        child.stderr.emit('data', Buffer.from('e1'));
        child.emit('close', 0);
        const result = await pending;
        assert.deepStrictEqual(seen, [
            { stream: 'stdout', text: 'a' },
            { stream: 'stdout', text: 'b' },
            { stream: 'stderr', text: 'e1' },
        ]);
        // The onChunk callback is additive — the aggregated result is unchanged.
        assert.strictEqual(result.stdout, 'ab');
        assert.strictEqual(result.stderr, 'e1');
    });
});

suite('DevcontainerProvisioner — resolveContainerWorkspaceFolder() (DEBT-085)', () => {
    /** Spawn stub: `up` emits no result line by default; a `pwd` exec answers PWD_ANSWER. */
    function harnessWithPwdAnswer(upStdout: string, pwdAnswer: string | null): Harness {
        const calls: SpawnCall[] = [];
        const children: FakeChild[] = [];
        const deps: ProvisionerDeps = {
            spawn: (command, args) => {
                const child = new FakeChild();
                calls.push({ command, args });
                children.push(child);
                const isPwd = args[args.length - 1] === 'pwd';
                if (upStdout && !isPwd) {
                    setTimeout(() => child.stdout.emit('data', Buffer.from(upStdout)), 0);
                }
                if (isPwd && pwdAnswer !== null) {
                    setTimeout(() => child.stdout.emit('data', Buffer.from(pwdAnswer)), 0);
                }
                const exitCode = isPwd && pwdAnswer === null ? 1 : 0;
                setTimeout(() => child.emit('close', exitCode), 0);
                return child as unknown as ChildProcess;
            },
            isExtensionInstalled: () => false,
            fileExists: () => true,
            log: () => { /* silent */ },
            bundledCliEntry: null,
        };
        return { deps, calls, children };
    }

    test('parses remoteWorkspaceFolder from up()\'s trailing JSON result line — no extra spawn', async () => {
        const jsonLine = `${JSON.stringify({ outcome: 'success', remoteWorkspaceFolder: '/workspaces/project' })}\n`;
        const h = harnessWithPwdAnswer(jsonLine, null);
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);
        const folder = await prov.resolveContainerWorkspaceFolder(ROOT);
        assert.strictEqual(folder, '/workspaces/project');
        assert.strictEqual(h.calls.length, 1, 'a pwd probe ran despite a usable JSON result line');
    });

    test('ignores intermixed non-JSON progress lines and takes the last valid JSON line', async () => {
        const stdout = 'Starting container...\nPulling image\n' +
            `${JSON.stringify({ remoteWorkspaceFolder: '/workspaces/project' })}\n`;
        const h = harnessWithPwdAnswer(stdout, null);
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);
        assert.strictEqual(await prov.resolveContainerWorkspaceFolder(ROOT), '/workspaces/project');
    });

    test('malformed/absent JSON falls back to a pwd probe, cached after first resolution', async () => {
        const h = harnessWithPwdAnswer('plain text, no JSON here\n', '/remote/workspace\n');
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);
        assert.strictEqual(h.calls.length, 1);

        const first = await prov.resolveContainerWorkspaceFolder(ROOT);
        assert.strictEqual(first, '/remote/workspace');
        assert.strictEqual(h.calls.length, 2, 'expected exactly one pwd probe spawn');

        const second = await prov.resolveContainerWorkspaceFolder(ROOT);
        assert.strictEqual(second, '/remote/workspace');
        assert.strictEqual(h.calls.length, 2, 'cached resolution must not spawn again');
    });

    test('a failed pwd probe is remembered — not retried on every call', async () => {
        const h = harnessWithPwdAnswer('plain text, no JSON here\n', null);
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);

        const first = await prov.resolveContainerWorkspaceFolder(ROOT);
        assert.strictEqual(first, undefined);
        assert.strictEqual(h.calls.length, 2);

        const second = await prov.resolveContainerWorkspaceFolder(ROOT);
        assert.strictEqual(second, undefined);
        assert.strictEqual(h.calls.length, 2, 'a failed probe must not be retried');
    });

    test('neither JSON nor pwd resolves — undefined, no crash', async () => {
        const h = harnessWithPwdAnswer('', null);
        const prov = new DevcontainerProvisioner(h.deps);
        await prov.up(ROOT);
        assert.strictEqual(await prov.resolveContainerWorkspaceFolder(ROOT), undefined);
    });
});
