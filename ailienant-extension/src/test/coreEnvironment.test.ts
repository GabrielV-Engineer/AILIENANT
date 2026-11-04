// Contract test for the Core setup pre-flight and the dashboard guard.
//
// coreEnvironment is vscode-free: the filesystem, process runner and HTTP probe
// are injected, so this runs without spawning Python or opening a socket.

import * as assert from 'assert';
import * as path from 'path';
import {
    CORE_LAUNCH_MODULE,
    DASHBOARD_ROUTE,
    VENV_DIR_NAMES,
    checkCoreEnvironment,
    checkDashboard,
    dashboardUrl,
    describeSetupProblem,
    resolveCorePython,
    venvPythonCandidates,
    type ProcessRunner,
} from '../providers/coreEnvironment';

const BACKEND = path.join('/repo', 'ailienant-core');

suite('coreEnvironment — interpreter resolution', () => {
    test('prefers a venv interpreter, in lookup order', () => {
        const candidates = venvPythonCandidates(BACKEND, 'linux');
        assert.strictEqual(candidates.length, VENV_DIR_NAMES.length);
        const second = candidates[1];
        const resolved = resolveCorePython(BACKEND, 'linux', (p) => p === second);
        assert.deepStrictEqual(resolved, { python: second, fromVenv: true });
    });

    test('Windows looks under Scripts/python.exe', () => {
        for (const candidate of venvPythonCandidates(BACKEND, 'win32')) {
            assert.ok(candidate.endsWith(path.join('Scripts', 'python.exe')), candidate);
        }
    });

    test('no venv falls back to the system interpreter, flagged as such', () => {
        assert.deepStrictEqual(resolveCorePython(BACKEND, 'linux', () => false), { python: 'python3', fromVenv: false });
        assert.deepStrictEqual(resolveCorePython(BACKEND, 'win32', () => false), { python: 'python', fromVenv: false });
    });
});

suite('coreEnvironment — pre-flight', () => {
    const runner = (code: number | null): { run: ProcessRunner; calls: string[][] } => {
        const calls: string[][] = [];
        return { calls, run: async (_cmd, args) => { calls.push(args); return code; } };
    };

    test('probes the same module the Core is launched with', async () => {
        const { run, calls } = runner(0);
        assert.strictEqual(await checkCoreEnvironment('python3', BACKEND, run), 'ok');
        assert.deepStrictEqual(calls, [['-c', `import ${CORE_LAUNCH_MODULE}`]]);
    });

    test('an interpreter that cannot start is reported as missing', async () => {
        assert.strictEqual(await checkCoreEnvironment('python3', BACKEND, runner(null).run), 'no-interpreter');
    });

    test('a failed import is reported as missing dependencies', async () => {
        assert.strictEqual(await checkCoreEnvironment('python3', BACKEND, runner(1).run), 'missing-dependencies');
    });

    test('without a venv, the fix includes creating one', () => {
        const text = describeSetupProblem('missing-dependencies', { python: 'python3', fromVenv: false }, BACKEND, 'linux');
        assert.match(text, /-m venv /);
        assert.match(text, /install -r requirements\.txt/);
        assert.ok(text.includes(BACKEND));
    });

    test('with a venv, the fix only installs dependencies', () => {
        const text = describeSetupProblem('missing-dependencies', { python: '/v/bin/python', fromVenv: true }, BACKEND, 'linux');
        assert.doesNotMatch(text, /-m venv /);
        assert.match(text, /install -r requirements\.txt/);
    });
});

suite('coreEnvironment — dashboard guard', () => {
    test('URL is built on the mounted route, with an optional tab', () => {
        assert.strictEqual(dashboardUrl('http://127.0.0.1:9/', ''), `http://127.0.0.1:9${DASHBOARD_ROUTE}`);
        assert.strictEqual(dashboardUrl('http://127.0.0.1:9', 'a b'), `http://127.0.0.1:9${DASHBOARD_ROUTE}?tab=a%20b`);
    });

    test('maps probe outcomes to availability', async () => {
        assert.strictEqual(await checkDashboard('u', async () => 200), 'ok');
        assert.strictEqual(await checkDashboard('u', async () => null), 'core-unreachable');
        assert.strictEqual(await checkDashboard('u', async () => 404), 'dashboard-not-built');
    });
});
