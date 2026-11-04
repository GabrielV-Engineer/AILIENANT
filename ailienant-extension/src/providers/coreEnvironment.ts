import * as cp from 'child_process';
import * as path from 'path';

/**
 * Pre-flight checks for running the Core from source, and the reachability
 * check behind the web dashboard button.
 *
 * Both exist so a missing setup step fails with a message that names the fix,
 * instead of a generic "Core stopped" after several futile retries, or a
 * browser tab pointed at a port nobody serves. vscode-free by design: every
 * process and network probe is injected, so this runs as a pure contract test.
 */

/** Module the Core is launched through (`python -m <module> main:app`). */
export const CORE_LAUNCH_MODULE = 'uvicorn';

/** Directory names a backend virtual environment is looked up under, in order. */
export const VENV_DIR_NAMES = ['.venv', 'venv'] as const;

// Importing the launch module is a few hundred ms; this bound only matters when
// the interpreter itself hangs (a broken shim, a stalled network filesystem).
export const PREFLIGHT_TIMEOUT_MS = 15_000;

// Matches APIClient.checkHealth()'s budget, so "reachable" means the same thing
// to the dashboard button as to the status pill.
export const DASHBOARD_PROBE_TIMEOUT_MS = 2000;

/** Path of the dashboard SPA on the Core, as mounted by `main.py`. */
export const DASHBOARD_ROUTE = '/dashboard/';

/** Where the setup steps live, relative to the repository root. */
export const SETUP_GUIDE_FILE = 'README.md';

export interface ResolvedPython {
    /** Interpreter to launch the Core with. */
    python: string;
    /** False when no backend venv exists and the system interpreter is the fallback. */
    fromVenv: boolean;
}

/** Venv interpreter paths for this platform, in lookup order. */
export function venvPythonCandidates(backendPath: string, platform: NodeJS.Platform): string[] {
    const tail = platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python'];
    return VENV_DIR_NAMES.map((dir) => path.join(backendPath, dir, ...tail));
}

export function resolveCorePython(
    backendPath: string,
    platform: NodeJS.Platform,
    exists: (p: string) => boolean,
): ResolvedPython {
    const found = venvPythonCandidates(backendPath, platform).find(exists);
    if (found) { return { python: found, fromVenv: true }; }
    return { python: platform === 'win32' ? 'python' : 'python3', fromVenv: false };
}

export type PreflightResult = 'ok' | 'no-interpreter' | 'missing-dependencies';

/** Runs the interpreter with args; resolves to its exit code, or null if it could not start. */
export type ProcessRunner = (cmd: string, args: string[], cwd: string) => Promise<number | null>;

export const defaultProcessRunner: ProcessRunner = (cmd, args, cwd) =>
    new Promise((resolve) => {
        cp.execFile(cmd, args, { cwd, timeout: PREFLIGHT_TIMEOUT_MS, windowsHide: true }, (err) => {
            if (!err) { resolve(0); return; }
            // A slow interpreter is not a broken one: report success and let the
            // normal readiness gate judge it, rather than block a valid setup.
            if (err.killed) { resolve(0); return; }
            // A non-numeric code (ENOENT, EACCES) means the interpreter never ran.
            resolve(typeof err.code === 'number' ? err.code : null);
        });
    });

/** Verify the interpreter exists and can import the Core's launch module. */
export async function checkCoreEnvironment(
    python: string,
    backendPath: string,
    run: ProcessRunner,
): Promise<PreflightResult> {
    const code = await run(python, ['-c', `import ${CORE_LAUNCH_MODULE}`], backendPath);
    if (code === null) { return 'no-interpreter'; }
    return code === 0 ? 'ok' : 'missing-dependencies';
}

/** Output-channel text naming the problem and the exact commands that fix it. */
export function describeSetupProblem(
    result: Exclude<PreflightResult, 'ok'>,
    resolved: ResolvedPython,
    backendPath: string,
    platform: NodeJS.Platform,
): string {
    const win = platform === 'win32';
    const venvDir = VENV_DIR_NAMES[VENV_DIR_NAMES.length - 1];
    const pip = win ? `.\\${venvDir}\\Scripts\\pip` : `${venvDir}/bin/pip`;
    const cause = result === 'no-interpreter'
        ? `Python interpreter "${resolved.python}" was not found.`
        : resolved.fromVenv
            ? `The backend virtual environment is missing its dependencies ("${CORE_LAUNCH_MODULE}" cannot be imported).`
            : `No backend virtual environment was found, and the system Python cannot import "${CORE_LAUNCH_MODULE}".`;
    return [
        `[AILIENANT] Core setup required: ${cause}`,
        '[AILIENANT] Run once, then use "AILIENANT: Restart Core":',
        `    cd "${backendPath}"`,
        ...(resolved.fromVenv ? [] : [`    ${win ? 'python' : 'python3'} -m venv ${venvDir}`]),
        `    ${pip} install -r requirements.txt`,
        `[AILIENANT] Full steps: ${SETUP_GUIDE_FILE} -> Quick start.`,
    ].join('\n');
}

export type DashboardAvailability = 'ok' | 'core-unreachable' | 'dashboard-not-built';

/** Fetches a URL; resolves to its HTTP status, or null when nothing answered. */
export type StatusProbe = (url: string) => Promise<number | null>;

export const defaultStatusProbe: StatusProbe = async (url) => {
    try {
        const response = await fetch(url, { signal: AbortSignal.timeout(DASHBOARD_PROBE_TIMEOUT_MS) });
        return response.status;
    } catch {
        return null;   // connection refused or timed out: the caller reports it
    }
};

export function dashboardUrl(base: string, tab: string): string {
    const root = `${base.replace(/\/$/, '')}${DASHBOARD_ROUTE}`;
    return tab ? `${root}?tab=${encodeURIComponent(tab)}` : root;
}

/**
 * Probe the dashboard itself rather than the health route: the Core mounts the
 * SPA only if its build output existed at startup, so a healthy Core can still
 * answer 404 here.
 */
export async function checkDashboard(url: string, probe: StatusProbe): Promise<DashboardAvailability> {
    const status = await probe(url);
    if (status === null) { return 'core-unreachable'; }
    if (status === 404) { return 'dashboard-not-built'; }
    return 'ok';
}
