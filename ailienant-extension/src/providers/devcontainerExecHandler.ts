// AILIENANT — host-side handler for the devcontainer execution bridge.
//
// The backend routes trusted provisioning + command execution to the host over
// two server→host events; this module drives the `DevcontainerProvisioner` and
// streams the results back as the three host→backend events. It is deliberately
// vscode-free: the workspace root, the message sender, the environment, and the
// "no devcontainer" hook are all injected, so it is a pure contract testable
// without a VS Code host (mirrors the provisioner's own DI discipline).

import * as path from 'path';
import type { DevcontainerStatus, ExecChunkListener, ExecResult } from './devcontainerProvisioner';

/** The subset of the provisioner this handler drives. */
export interface ProvisionerLike {
    up(workspaceRoot: string): Promise<DevcontainerStatus>;
    exec(
        workspaceRoot: string,
        command: string,
        env?: NodeJS.ProcessEnv,
        timeoutMs?: number,
        containerCwd?: string,
        onChunk?: ExecChunkListener,
    ): Promise<ExecResult>;
    /** Resolve the container-side workspace root (DEBT-085), or `undefined` when unknown. */
    resolveContainerWorkspaceFolder(workspaceRoot: string): Promise<string | undefined>;
}

// Coalescing window + per-frame cap for DEBT-083 incremental streaming. Sized
// against the bandwidth-delay product the D2 (session tunnel) backpressure
// design depends on: ~50ms coalescing caps a stream at ~20 frames/s, so a
// ~100ms worst-case local RTT never has more than 2-3 frames in flight when a
// downstream consumer signals it needs to pause — see devcontainerSessionHandler.ts.
const _STREAM_COALESCE_MS = 50;
const _STREAM_CHUNK_CAP_BYTES = 8192;

type StreamName = 'stdout' | 'stderr';

/**
 * Coalesce a chattering `onChunk` stream into a handful of WS frames instead of
 * one per OS `data` event (DEBT-083). Each of stdout/stderr buffers
 * independently and flushes on whichever trips first: the ~50ms timer or the
 * byte cap. `flush()` is synchronous and must be called after the owning
 * `exec()` resolves, and BEFORE the terminal exit frame is sent — a stream
 * frame arriving after the exit frame is dropped by the backend's
 * `append_devcontainer_stream` (unknown-`request_id` branch), so residue left
 * unflushed would silently vanish.
 */
function makeStreamCoalescer(
    emit: (stream: StreamName, chunk: string) => void,
): { onChunk: ExecChunkListener; flush: () => void } {
    const buffers: Record<StreamName, string> = { stdout: '', stderr: '' };
    const timers: Record<StreamName, ReturnType<typeof setTimeout> | null> = { stdout: null, stderr: null };

    const flushStream = (stream: StreamName): void => {
        const timer = timers[stream];
        if (timer) {
            clearTimeout(timer);
            timers[stream] = null;
        }
        if (buffers[stream]) {
            emit(stream, buffers[stream]);
            buffers[stream] = '';
        }
    };

    return {
        onChunk: (stream, text) => {
            buffers[stream] += text;
            if (buffers[stream].length >= _STREAM_CHUNK_CAP_BYTES) {
                flushStream(stream);
                return;
            }
            if (!timers[stream]) {
                timers[stream] = setTimeout(() => flushStream(stream), _STREAM_COALESCE_MS);
            }
        },
        flush: () => {
            flushStream('stdout');
            flushStream('stderr');
        },
    };
}

export interface DevcontainerHandlerDeps {
    provisioner: ProvisionerLike;
    /** Absolute path of the workspace folder, or undefined when none is open. */
    workspaceRoot: string | undefined;
    /** Sends a client→backend event over the WS channel. */
    send: (message: { event_type: string; data: unknown }) => void;
    /** Host environment the whitelisted `env_keys` are resolved against. */
    env: NodeJS.ProcessEnv;
    /** Best-effort hook fired when provisioning fails for a missing devcontainer.json. */
    onNoDevcontainer?: () => void;
    log?: (message: string) => void;
}

type ProvisionState = 'provisioning' | 'ready' | 'timeout' | 'failed';

/** Map the provisioner's internal state to the wire's terminal provision state. */
function toWireState(status: DevcontainerStatus): ProvisionState {
    if (status.state === 'ready') { return 'ready'; }
    if (/timed out|timeout/i.test(status.detail ?? '')) { return 'timeout'; }
    return 'failed';
}

/** True when a degrade was caused by the workspace lacking a devcontainer.json. */
function isMissingConfig(status: DevcontainerStatus): boolean {
    return /devcontainer\.json/i.test(status.detail ?? '');
}

/**
 * Map a backend `cwd` (a host path) onto the container workspace (DEBT-085).
 *
 * Returns `undefined` — meaning "run unprefixed, at the container workspace
 * root" — whenever translation is not knowable or not safe: no workspace root,
 * an empty `cwd`, a `cwd` equal to the root, a relative path escaping the root
 * (`..`), or a path `path.relative` cannot express as relative (e.g. a
 * different drive on Windows). This is a confinement floor, not a best-effort
 * convenience — never translate a path outside the workspace mount.
 */
async function resolveContainerCwd(
    deps: DevcontainerHandlerDeps,
    hostCwd: string,
): Promise<string | undefined> {
    if (!deps.workspaceRoot || !hostCwd) {
        return undefined;
    }
    const containerRoot = await deps.provisioner.resolveContainerWorkspaceFolder(deps.workspaceRoot);
    if (!containerRoot) {
        return undefined;
    }
    const rel = path.relative(deps.workspaceRoot, hostCwd);
    if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) {
        return undefined;
    }
    // Normalize host separators (Windows emits `src\api`) to the POSIX form the
    // container path needs before joining onto the resolved container root.
    const posixRel = rel.split(path.sep).join('/');
    return path.posix.join(containerRoot, posixRel);
}

/**
 * Handle a server→host devcontainer event. Returns true when the message was one
 * of ours (and was handled), false otherwise so the caller can keep routing.
 */
export async function handleDevcontainerServerEvent(
    msg: { event_type?: string; data?: unknown },
    deps: DevcontainerHandlerDeps,
): Promise<boolean> {
    if (msg.event_type === 'server_devcontainer_provision_request') {
        const data = msg.data as { session_id: string; request_id: string; cwd: string };
        if (!deps.workspaceRoot) {
            deps.send({
                event_type: 'client_devcontainer_provision_status',
                data: { session_id: data.session_id, request_id: data.request_id, state: 'failed' },
            });
            return true;
        }
        const status = await deps.provisioner.up(deps.workspaceRoot);
        if (status.state !== 'ready' && isMissingConfig(status)) {
            deps.onNoDevcontainer?.();
        }
        deps.send({
            event_type: 'client_devcontainer_provision_status',
            data: {
                session_id: data.session_id,
                request_id: data.request_id,
                state: toWireState(status),
            },
        });
        return true;
    }

    if (msg.event_type === 'server_devcontainer_exec_request') {
        const data = msg.data as {
            session_id: string; request_id: string; command: string;
            cwd: string; env_keys: string[];
        };
        const { session_id, request_id } = data;
        if (!deps.workspaceRoot) {
            deps.send({
                event_type: 'client_devcontainer_exec_exit',
                data: { session_id, request_id, exit_code: -1 },
            });
            return true;
        }
        // Resolve allowlisted NAMES to values from the host environment.
        const env: NodeJS.ProcessEnv = {};
        for (const key of data.env_keys ?? []) {
            const value = deps.env[key];
            if (value !== undefined) { env[key] = value; }
        }

        const containerCwd = await resolveContainerCwd(deps, data.cwd);

        // True incremental streaming (DEBT-083): each raw data event is coalesced
        // and forwarded as its own client_devcontainer_exec_stream frame while the
        // command is still running, rather than buffered into one emit at the end.
        const coalescer = makeStreamCoalescer((stream, chunk) => {
            deps.send({
                event_type: 'client_devcontainer_exec_stream',
                data: { session_id, request_id, stream, chunk },
            });
        });

        let result: ExecResult;
        try {
            result = await deps.provisioner.exec(
                deps.workspaceRoot, data.command, env, undefined, containerCwd,
                coalescer.onChunk,
            );
        } catch (err) {
            // Flush whatever streamed before the failure so no output is lost —
            // ordering still holds: this runs before the exit frame below.
            coalescer.flush();
            deps.log?.(`devcontainer exec threw: ${err instanceof Error ? err.message : String(err)}`);
            deps.send({
                event_type: 'client_devcontainer_exec_exit',
                data: { session_id, request_id, exit_code: -1 },
            });
            return true;
        }

        // Flush any coalesced residue BEFORE the exit frame — the exit frame is
        // what resolves the backend's exec waiter, and a stream chunk arriving
        // after it is dropped by append_devcontainer_stream's unknown-request_id
        // branch, so this ordering is load-bearing, not cosmetic.
        coalescer.flush();
        deps.send({
            event_type: 'client_devcontainer_exec_exit',
            data: { session_id, request_id, exit_code: result.exitCode ?? -1 },
        });
        return true;
    }

    return false;
}
