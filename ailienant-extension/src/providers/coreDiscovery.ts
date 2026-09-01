import * as cp from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { logger } from '../shared/logger';

/**
 * Discovery of an already-running Core, and the process-tree kill primitive.
 *
 * Two independent concerns live here because both are pure-ish host mechanics
 * that must stay testable without constructing a WebviewPanel or spawning a
 * real process:
 *
 * 1. Reading the run-state file the backend publishes at startup, so a Core
 *    that is already serving can be adopted rather than duplicated. The file is
 *    a hint, never a truth — a crash leaves it pointing at a dead port — so
 *    every consumer probes liveness before acting on it.
 * 2. Killing a spawned Core by its whole process tree. `python -m uvicorn`
 *    forks a worker that owns the listening socket; targeting the tree rather
 *    than the launcher alone means a stop cannot leave that worker holding the
 *    port, whatever the shutdown path.
 */

// Matches the probe budget APIClient.checkHealth() uses, so "adoptable" and
// "healthy" can never disagree about the same backend.
const DISCOVERY_PROBE_TIMEOUT_MS = 2000;

// A cold `import main` measures ~5s with a warm file cache and materially
// longer on the first start after a reboot, when that cache is empty.
export const CORE_READY_TIMEOUT_MS = 45_000;
export const CORE_READY_POLL_INTERVAL_MS = 500;

// Upper bound on how long stop() may wait for a killed process to report exit.
// Past this the process is assumed unreapable and the caller proceeds: stop()
// must never become the thing that wedges a restart.
export const CORE_STOP_TIMEOUT_MS = 5000;

// Grace period between SIGTERM and SIGKILL on POSIX, matching the escalation
// the spawned-process path used before the tree kill replaced it.
const POSIX_SIGKILL_DELAY_MS = 3000;

// `taskkill` exit code for "the process is not running". A target that is
// already gone is the outcome we wanted, so it is success, not failure — this
// is what makes repeated Restart Core clicks idempotent.
const TASKKILL_NOT_FOUND = 128;

/** Loopback coordinates of a running Core, as published in the run-state file. */
export interface RunState {
    port: number;
    token: string | null;
    pid: number;
}

/**
 * Absolute path of the run-state file, derived from the same home-relative
 * location the backend writes to rather than restated as a literal.
 */
export function runStatePath(): string {
    return path.join(os.homedir(), '.ailienant', 'run.json');
}

/**
 * Parse run-state JSON. Pure — no filesystem — so every malformed-input branch
 * is unit-testable. Returns null for anything that cannot address a host.
 *
 * `token` is optional by contract: a backend started without an auth token
 * publishes `null` there. Unknown keys are ignored so the file can gain fields
 * without breaking older readers.
 */
export function parseRunState(raw: string): RunState | null {
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return null;
    }
    if (typeof parsed !== 'object' || parsed === null) { return null; }
    const obj = parsed as Record<string, unknown>;
    const { port, pid, token } = obj;
    if (!Number.isInteger(port) || !Number.isInteger(pid)) { return null; }
    if (token !== undefined && token !== null && typeof token !== 'string') { return null; }
    return {
        port: port as number,
        pid: pid as number,
        token: typeof token === 'string' ? token : null,
    };
}

/** Read and parse the run-state file. Absence is the normal case, not an error. */
export async function readRunState(): Promise<RunState | null> {
    try {
        return parseRunState(await fs.promises.readFile(runStatePath(), 'utf-8'));
    } catch (err) {
        // A missing file just means no Core has published coordinates.
        if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') { return null; }
        // noqa-equivalent: discovery is best-effort and must never block
        // activation, but an unreadable file (permissions, corruption) is a real
        // fault worth surfacing rather than swallowing.
        logger.error('[core] Could not read run-state file', err);
        return null;
    }
}

/**
 * Probe whether the recorded host is actually serving.
 *
 * Deliberately stronger than the backend's own TCP-connect probe: an HTTP
 * `GET /` proves the application is answering, not merely that something holds
 * the socket. `fetchImpl` is injectable so the failure branches are testable
 * without a network.
 */
export async function probeRunState(
    state: RunState,
    fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
    try {
        const response = await fetchImpl(`http://127.0.0.1:${state.port}/`, {
            method: 'GET',
            signal: AbortSignal.timeout(DISCOVERY_PROBE_TIMEOUT_MS),
        });
        return response.ok;
    } catch {
        // A dead port refuses the connection; that is a negative probe result,
        // not an error to propagate.
        return false;
    }
}

/**
 * Adoption policy: whether discovered coordinates may be taken over.
 *
 * A token-less run state is refused deliberately. It means the backend was
 * started without an auth token — a manual or standalone start whose lifecycle
 * this extension does not own and whose identity it cannot verify.
 */
export function shouldAdopt(state: RunState | null, probeOk: boolean): boolean {
    return state !== null && probeOk && typeof state.token === 'string' && state.token.length > 0;
}

/** Lifecycle states a managed Core can be in. */
export type CoreState = 'stopped' | 'starting' | 'running' | 'crashed';

/**
 * Decide what a finished readiness probe may write to the manager's state.
 *
 * The crash-retry handler also moves the state to 'starting' and schedules a
 * fresh spawn, so an in-flight probe belonging to an already-superseded attempt
 * must not stamp 'crashed' over that retry. Identity of the process being
 * probed — not the state alone — is what distinguishes the two, since both the
 * original attempt and its retry sit in 'starting'.
 *
 * Returns the state to adopt, or null to leave the current state untouched.
 */
export function resolveReadyOutcome(args: {
    procIsCurrent: boolean;
    healthy: boolean;
    state: CoreState;
}): CoreState | null {
    if (!args.procIsCurrent) { return null; }   // a newer attempt owns the state
    if (args.healthy) { return 'running'; }
    if (args.state !== 'starting') { return null; }   // stopped or already terminal
    return 'crashed';
}

/** Argv for the platform's tree kill, or null where signals are used instead. */
export function buildKillCommand(
    pid: number,
    platform: NodeJS.Platform,
): { cmd: string; args: string[] } | null {
    if (platform === 'win32') {
        // /T covers the whole tree (the uvicorn launcher AND the worker that owns
        // the port), /F forces it, so no descendant can outlive the stop.
        return { cmd: 'taskkill', args: ['/PID', String(pid), '/T', '/F'] };
    }
    return null;
}

/**
 * Terminate a process and its descendants. Never throws and always resolves —
 * callers use it on cleanup paths where a rejection would strand state.
 */
export function killProcessTree(pid: number, log: (line: string) => void): Promise<void> {
    const command = buildKillCommand(pid, process.platform);

    if (command) {
        return new Promise<void>((resolve) => {
            const settle = (): void => resolve();
            const killer = cp.spawn(command.cmd, command.args, { windowsHide: true });
            killer.on('close', (code) => {
                if (code !== 0 && code !== TASKKILL_NOT_FOUND) {
                    log(`[AILIENANT] taskkill for pid ${pid} exited with code ${code}.`);
                }
                settle();
            });
            killer.on('error', (err) => {
                log(`[AILIENANT] Could not run taskkill for pid ${pid}: ${err.message}`);
                settle();
            });
        });
    }

    return new Promise<void>((resolve) => {
        try {
            process.kill(pid, 'SIGTERM');
        } catch (err) {
            // ESRCH: already gone, which is the desired end state.
            if ((err as NodeJS.ErrnoException)?.code !== 'ESRCH') {
                log(`[AILIENANT] Could not signal pid ${pid}: ${String(err)}`);
            }
            resolve();
            return;
        }
        setTimeout(() => {
            try {
                process.kill(pid, 0);       // throws ESRCH once the process is gone
                process.kill(pid, 'SIGKILL');
            } catch {
                // Already exited after SIGTERM — nothing left to escalate to.
            }
            resolve();
        }, POSIX_SIGKILL_DELAY_MS);
    });
}

/**
 * Resolve `p`, or invoke `onTimeout` and resolve with its value once `ms`
 * elapses. Never rejects: the shared idiom for awaits that must not hang.
 */
export function withTimeout<T>(p: Promise<T>, ms: number, onTimeout: () => T): Promise<T> {
    return new Promise<T>((resolve) => {
        let settled = false;
        const timer = setTimeout(() => {
            if (settled) { return; }
            settled = true;
            resolve(onTimeout());
        }, ms);
        void p.then(
            (value) => {
                if (settled) { return; }
                settled = true;
                clearTimeout(timer);
                resolve(value);
            },
            () => {
                if (settled) { return; }
                settled = true;
                clearTimeout(timer);
                resolve(onTimeout());
            },
        );
    });
}
