// AILIENANT — host-side handler for the devcontainer execution bridge.
//
// The backend routes trusted provisioning + command execution to the host over
// two server→host events; this module drives the `DevcontainerProvisioner` and
// streams the results back as the three host→backend events. It is deliberately
// vscode-free: the workspace root, the message sender, the environment, and the
// "no devcontainer" hook are all injected, so it is a pure contract testable
// without a VS Code host (mirrors the provisioner's own DI discipline).

import type { DevcontainerStatus, ExecResult } from './devcontainerProvisioner';

/** The subset of the provisioner this handler drives. */
export interface ProvisionerLike {
    up(workspaceRoot: string): Promise<DevcontainerStatus>;
    exec(
        workspaceRoot: string,
        command: string,
        env?: NodeJS.ProcessEnv,
    ): Promise<ExecResult>;
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

        let result: ExecResult;
        try {
            result = await deps.provisioner.exec(deps.workspaceRoot, data.command, env);
        } catch (err) {
            deps.log?.(`devcontainer exec threw: ${err instanceof Error ? err.message : String(err)}`);
            deps.send({
                event_type: 'client_devcontainer_exec_exit',
                data: { session_id, request_id, exit_code: -1 },
            });
            return true;
        }

        // Buffered single-emit per stream — the backend aggregates chunks until
        // the exit frame, so one chunk each is contract-equivalent.
        if (result.stdout) {
            deps.send({
                event_type: 'client_devcontainer_exec_stream',
                data: { session_id, request_id, stream: 'stdout', chunk: result.stdout },
            });
        }
        if (result.stderr) {
            deps.send({
                event_type: 'client_devcontainer_exec_stream',
                data: { session_id, request_id, stream: 'stderr', chunk: result.stderr },
            });
        }
        deps.send({
            event_type: 'client_devcontainer_exec_exit',
            data: { session_id, request_id, exit_code: result.exitCode ?? -1 },
        });
        return true;
    }

    return false;
}
