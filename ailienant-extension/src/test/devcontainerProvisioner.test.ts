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
});
