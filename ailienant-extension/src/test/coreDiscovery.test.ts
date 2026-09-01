/**
 * Core discovery, adoption policy, and the kill/timeout primitives.
 *
 * These guard the recovery path for a Core that outlives its extension host:
 * the run-state file it left behind is a hint that may point at a dead port, so
 * every branch that decides whether to trust it — and every primitive that must
 * not hang or throw while cleaning up — is asserted here without spawning a
 * process or touching the network.
 */
import * as assert from 'assert';
import * as os from 'os';
import * as path from 'path';
import {
    buildKillCommand,
    parseRunState,
    probeRunState,
    resolveReadyOutcome,
    runStatePath,
    shouldAdopt,
    withTimeout,
    type RunState,
} from '../providers/coreDiscovery';

const VALID = JSON.stringify({ port: 61171, token: 'abc123', pid: 14296 });

suite('coreDiscovery — parseRunState', () => {
    test('parses a well-formed run state', () => {
        const state = parseRunState(VALID);
        assert.deepStrictEqual(state, { port: 61171, token: 'abc123', pid: 14296 });
    });

    test('malformed JSON yields null rather than throwing', () => {
        assert.strictEqual(parseRunState('{not json'), null);
        assert.strictEqual(parseRunState(''), null);
    });

    test('a non-object payload yields null', () => {
        assert.strictEqual(parseRunState('null'), null);
        assert.strictEqual(parseRunState('42'), null);
    });

    test('missing port or pid yields null — neither can address a host', () => {
        assert.strictEqual(parseRunState(JSON.stringify({ token: 'a', pid: 1 })), null);
        assert.strictEqual(parseRunState(JSON.stringify({ port: 8000, token: 'a' })), null);
    });

    test('a null token still parses — the adoption gate rejects it separately', () => {
        const state = parseRunState(JSON.stringify({ port: 8000, token: null, pid: 7 }));
        assert.deepStrictEqual(state, { port: 8000, token: null, pid: 7 });
    });

    test('unknown keys are tolerated so the file can gain fields', () => {
        const state = parseRunState(
            JSON.stringify({ port: 8000, token: 'a', pid: 7, workspace_root: '/x' }),
        );
        assert.deepStrictEqual(state, { port: 8000, token: 'a', pid: 7 });
    });
});

suite('coreDiscovery — runStatePath', () => {
    test('resolves under the user home, derived rather than restated', () => {
        assert.strictEqual(runStatePath(), path.join(os.homedir(), '.ailienant', 'run.json'));
    });
});

suite('coreDiscovery — probeRunState', () => {
    const state: RunState = { port: 61171, token: 'abc', pid: 1 };

    test('an ok response means the host is serving', async () => {
        const fetchImpl = (async () => ({ ok: true })) as unknown as typeof fetch;
        assert.strictEqual(await probeRunState(state, fetchImpl), true);
    });

    test('a non-ok response means it is not adoptable', async () => {
        const fetchImpl = (async () => ({ ok: false })) as unknown as typeof fetch;
        assert.strictEqual(await probeRunState(state, fetchImpl), false);
    });

    test('a refused connection is a negative result, not a thrown error', async () => {
        const fetchImpl = (async () => {
            throw Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' });
        }) as unknown as typeof fetch;
        assert.strictEqual(await probeRunState(state, fetchImpl), false);
    });
});

suite('coreDiscovery — shouldAdopt', () => {
    const healthy: RunState = { port: 61171, token: 'abc', pid: 1 };

    test('adopts a probed, token-carrying host', () => {
        assert.strictEqual(shouldAdopt(healthy, true), true);
    });

    test('refuses when there is no run state at all', () => {
        assert.strictEqual(shouldAdopt(null, true), false);
    });

    test('refuses a stale file whose port no longer answers', () => {
        assert.strictEqual(shouldAdopt(healthy, false), false);
    });

    test('refuses a token-less host — its lifecycle is not ours to drive', () => {
        assert.strictEqual(shouldAdopt({ port: 8000, token: null, pid: 1 }, true), false);
        assert.strictEqual(shouldAdopt({ port: 8000, token: '', pid: 1 }, true), false);
    });
});

suite('coreDiscovery — resolveReadyOutcome', () => {
    test('a healthy current process becomes running', () => {
        assert.strictEqual(
            resolveReadyOutcome({ procIsCurrent: true, healthy: true, state: 'starting' }),
            'running',
        );
    });

    test('an unhealthy current process still starting becomes crashed', () => {
        assert.strictEqual(
            resolveReadyOutcome({ procIsCurrent: true, healthy: false, state: 'starting' }),
            'crashed',
        );
    });

    test('a superseded attempt writes nothing — the retry owns the state', () => {
        assert.strictEqual(
            resolveReadyOutcome({ procIsCurrent: false, healthy: false, state: 'starting' }),
            null,
        );
        assert.strictEqual(
            resolveReadyOutcome({ procIsCurrent: false, healthy: true, state: 'starting' }),
            null,
        );
    });

    test('a deliberate stop is never overwritten by a late probe', () => {
        assert.strictEqual(
            resolveReadyOutcome({ procIsCurrent: true, healthy: false, state: 'stopped' }),
            null,
        );
    });
});

suite('coreDiscovery — buildKillCommand', () => {
    test('windows kills the whole tree, which is what reaps the uvicorn worker', () => {
        assert.deepStrictEqual(buildKillCommand(4242, 'win32'), {
            cmd: 'taskkill',
            args: ['/PID', '4242', '/T', '/F'],
        });
    });

    test('posix uses signals instead of a helper process', () => {
        assert.strictEqual(buildKillCommand(4242, 'linux'), null);
        assert.strictEqual(buildKillCommand(4242, 'darwin'), null);
    });
});

suite('coreDiscovery — withTimeout', () => {
    test('resolves with the inner value when it arrives in time', async () => {
        assert.strictEqual(await withTimeout(Promise.resolve('ok'), 1000, () => 'timed-out'), 'ok');
    });

    test('falls back once the budget elapses', async () => {
        const never = new Promise<string>(() => { /* never settles */ });
        assert.strictEqual(await withTimeout(never, 20, () => 'timed-out'), 'timed-out');
    });

    test('a rejection resolves to the fallback rather than propagating', async () => {
        const rejected = Promise.reject(new Error('boom'));
        assert.strictEqual(await withTimeout(rejected, 1000, () => 'fallback'), 'fallback');
    });
});
