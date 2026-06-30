// AILIENANT — Devcontainer provisioning driver (extension lifecycle owner).
//
// Owns the host side of trusted project execution: it probes for a usable
// `devcontainer` CLI, drives `devcontainer up` / `devcontainer exec` against the
// user's local Docker daemon, and exposes provisioning status. Execution is
// always a child process of the CLI — the Dev Containers extension is treated as
// a capability/binary source, not a headless run mechanism (its commands reopen
// the whole window inside a container, the wrong model for a host-resident agent).
//
// The core imports no `vscode`: every host-specific capability (spawn, extension
// probe, filesystem, logging, the resolved bundled-CLI entry) is injected, so the
// state machine is a pure contract testable without a real VS Code host. The
// `extension.ts` factory wires the real implementations.
//
// Distribution: the runnable `devcontainer` CLI is a HOST PREREQUISITE — it comes
// from the user's PATH or the installed Dev Containers extension. The
// `@devcontainers/cli` package is a dev/test convenience only and is never shipped
// in the packaged extension (the build excludes `node_modules`), so when neither a
// PATH binary nor the extension is present the driver degrades with an actionable
// remediation message rather than silently failing.

import * as path from 'path';
import type { ChildProcess } from 'child_process';

export const DEVCONTAINERS_EXTENSION_ID = 'ms-vscode-remote.remote-containers';

/** Where the runnable `devcontainer` CLI came from (for status reporting). */
export type DevcontainerCliSource = 'devcontainers-extension' | 'bundled-dep' | 'path';

/** Provisioning lifecycle state. */
export type DevcontainerState = 'idle' | 'provisioning' | 'ready' | 'degraded';

export interface DevcontainerStatus {
    state: DevcontainerState;
    cliSource: DevcontainerCliSource;
    detail?: string;
}

export interface ExecResult {
    stdout: string;
    stderr: string;
    exitCode: number | null;
}

interface ResolvedCli {
    command: string;
    baseArgs: string[];
    source: DevcontainerCliSource;
}

/** Subset of `child_process.spawn` the driver depends on. */
export type SpawnFn = (
    command: string,
    args: string[],
    options: { cwd?: string; env?: NodeJS.ProcessEnv },
) => ChildProcess;

/** Host capabilities injected by the extension factory (kept the core vscode-free). */
export interface ProvisionerDeps {
    spawn: SpawnFn;
    isExtensionInstalled: (id: string) => boolean;
    fileExists: (p: string) => boolean;
    log: (message: string) => void;
    /** Absolute path to the bundled `@devcontainers/cli` entry, or null when unresolved. */
    bundledCliEntry: string | null;
    /** Node executable used to run the bundled CLI .js entry. Defaults to process.execPath. */
    nodePath?: string;
    /** Override the provisioning wall-clock budget (ms). Defaults to PROVISION_TIMEOUT_MS. */
    provisionTimeoutMs?: number;
}

const PROVISION_TIMEOUT_MS = 10 * 60 * 1000;   // devcontainer up = image build + caching
const KILL_GRACE_MS = 3000;                    // SIGTERM → SIGKILL escalation window

// Shown when no usable CLI was found (no Dev Containers extension, no bundled dep,
// and a PATH spawn would ENOENT). Names both supported ways to provide the CLI.
const CLI_MISSING_HINT =
    'devcontainer CLI not found — install the "Dev Containers" extension ' +
    '(ms-vscode-remote.remote-containers) or put a `devcontainer` binary on your PATH.';

type StatusListener = (status: DevcontainerStatus) => void;

/**
 * Lazy, single-flight, idempotent devcontainer lifecycle owner. Never blocks the
 * extension-host event loop (all work is async child processes) and never hangs
 * (every spawn is bounded by a timeout that escalates SIGTERM → SIGKILL).
 */
export class DevcontainerProvisioner {
    private _state: DevcontainerState = 'idle';
    private _detail: string | undefined;
    private _upInFlight: Promise<DevcontainerStatus> | null = null;
    private _activeChild: ChildProcess | null = null;
    private readonly _listeners = new Set<StatusListener>();

    constructor(private readonly deps: ProvisionerDeps) {}

    getStatus(): DevcontainerStatus {
        return { state: this._state, cliSource: this.resolveCli().source, detail: this._detail };
    }

    /** Subscribe to status transitions; returns an unsubscribe function. */
    onDidChangeStatus(listener: StatusListener): () => void {
        this._listeners.add(listener);
        return () => this._listeners.delete(listener);
    }

    /** True when `<root>/.devcontainer/devcontainer.json` or `<root>/.devcontainer.json` exists. */
    hasDevcontainerConfig(workspaceRoot: string): boolean {
        return (
            this.deps.fileExists(path.join(workspaceRoot, '.devcontainer', 'devcontainer.json')) ||
            this.deps.fileExists(path.join(workspaceRoot, '.devcontainer.json'))
        );
    }

    /**
     * Resolve which `devcontainer` CLI binary to spawn and where it came from.
     * Precedence (cheaply checkable): Dev Containers extension → our bundled dep
     * → PATH fallback (verified at spawn time, which degrades on ENOENT).
     */
    resolveCli(): ResolvedCli {
        if (this.deps.isExtensionInstalled(DEVCONTAINERS_EXTENSION_ID)) {
            return { command: 'devcontainer', baseArgs: [], source: 'devcontainers-extension' };
        }
        if (this.deps.bundledCliEntry) {
            const node = this.deps.nodePath ?? process.execPath;
            return { command: node, baseArgs: [this.deps.bundledCliEntry], source: 'bundled-dep' };
        }
        return { command: 'devcontainer', baseArgs: [], source: 'path' };
    }

    /**
     * Bring the workspace container up — lazy, single-flight, idempotent.
     * A second call while `ready` is a cached no-op; concurrent callers share one
     * in-flight provision. A timeout or non-zero exit degrades cleanly.
     */
    up(workspaceRoot: string): Promise<DevcontainerStatus> {
        if (this._state === 'ready') {
            return Promise.resolve(this.getStatus());
        }
        if (this._upInFlight) {
            return this._upInFlight;
        }
        this._upInFlight = this._doUp(workspaceRoot).finally(() => {
            this._upInFlight = null;
        });
        return this._upInFlight;
    }

    private async _doUp(workspaceRoot: string): Promise<DevcontainerStatus> {
        if (!this.hasDevcontainerConfig(workspaceRoot)) {
            this._setState('degraded', 'no devcontainer.json in workspace');
            return this.getStatus();
        }
        const cli = this.resolveCli();
        // No extension and no bundled dep: we are relying on a PATH binary that may
        // not exist. Surface the remediation up front so a later ENOENT is actionable.
        if (cli.source === 'path') {
            this.deps.log(CLI_MISSING_HINT);
        }
        this._setState('provisioning');
        try {
            const result = await this._spawnWithTimeout(
                cli, ['up', '--workspace-folder', workspaceRoot], workspaceRoot,
                this.deps.provisionTimeoutMs ?? PROVISION_TIMEOUT_MS,
            );
            if (result.exitCode === 0) {
                this._setState('ready');
            } else {
                this._setState('degraded', `devcontainer up exited ${result.exitCode}`);
            }
        } catch (err) {
            // A missing CLI (ENOENT) gets the actionable remediation; other failures
            // report their root cause verbatim.
            const detail = isCliMissing(err)
                ? CLI_MISSING_HINT
                : `devcontainer up failed: ${errText(err)}`;
            this._setState('degraded', detail);
        }
        return this.getStatus();
    }

    /**
     * Run a command inside the provisioned container. The command string is
     * executed by the container's shell (`/bin/sh -c`) — the host spawn itself is
     * argv-array with no host shell, so no host-side command injection is possible.
     * `env` is applied host-side when launching the CLI.
     */
    async exec(
        workspaceRoot: string,
        command: string,
        env?: NodeJS.ProcessEnv,
        timeoutMs?: number,
    ): Promise<ExecResult> {
        const cli = this.resolveCli();
        const budget = timeoutMs ?? this.deps.provisionTimeoutMs ?? PROVISION_TIMEOUT_MS;
        return this._spawnWithTimeout(
            cli,
            ['exec', '--workspace-folder', workspaceRoot, '--', '/bin/sh', '-c', command],
            workspaceRoot, budget, env,
        );
    }

    /** Kill any in-flight child. Idempotent. */
    dispose(): void {
        this._killActiveChild();
        this._listeners.clear();
    }

    // ── internals ────────────────────────────────────────────────────────────

    private _spawnWithTimeout(
        cli: ResolvedCli,
        args: string[],
        cwd: string,
        timeoutMs: number,
        env?: NodeJS.ProcessEnv,
    ): Promise<ExecResult> {
        return new Promise<ExecResult>((resolve, reject) => {
            const child = this.deps.spawn(cli.command, [...cli.baseArgs, ...args], { cwd, env });
            this._activeChild = child;

            let stdout = '';
            let stderr = '';
            let settled = false;

            const finish = (fn: () => void): void => {
                if (settled) { return; }
                settled = true;
                clearTimeout(timer);
                if (this._activeChild === child) { this._activeChild = null; }
                fn();
            };

            const timer = setTimeout(() => {
                finish(() => {
                    this._killChild(child);
                    reject(new Error(`devcontainer timed out after ${timeoutMs}ms`));
                });
            }, timeoutMs);

            child.stdout?.on('data', (c: Buffer) => { stdout += c.toString(); });
            child.stderr?.on('data', (c: Buffer) => { stderr += c.toString(); });
            child.on('error', (err: Error) => finish(() => reject(err)));
            child.on('close', (code: number | null) => finish(() => resolve({ stdout, stderr, exitCode: code })));
        });
    }

    private _killActiveChild(): void {
        if (this._activeChild) {
            this._killChild(this._activeChild);
            this._activeChild = null;
        }
    }

    private _killChild(child: ChildProcess): void {
        // Mirrors CoreProcessManager.stop: hard kill on Windows (no signal
        // semantics), SIGTERM → SIGKILL escalation elsewhere.
        if (process.platform === 'win32') {
            try { child.kill(); } catch { /* already dead */ }
            return;
        }
        try { child.kill('SIGTERM'); } catch { /* already dead */ }
        // unref so this cleanup-only timer never keeps the host event loop alive.
        setTimeout(() => {
            try { child.kill('SIGKILL'); } catch { /* already dead */ }
        }, KILL_GRACE_MS).unref();
    }

    private _setState(state: DevcontainerState, detail?: string): void {
        this._state = state;
        this._detail = detail;
        this.deps.log(`state=${state}${detail ? ` (${detail})` : ''}`);
        const snapshot = this.getStatus();
        for (const listener of this._listeners) {
            try {
                listener(snapshot);
            } catch (err) {
                this.deps.log(`status listener threw: ${errText(err)}`);
            }
        }
    }
}

function errText(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
}

/** True when a spawn failure indicates the `devcontainer` binary was not found. */
function isCliMissing(err: unknown): boolean {
    if (err instanceof Error) {
        if ((err as NodeJS.ErrnoException).code === 'ENOENT') { return true; }
        return /ENOENT/.test(err.message);
    }
    return false;
}
