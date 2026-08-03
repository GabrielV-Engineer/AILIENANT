// AILIENANT — host-side handler for the devcontainer interactive session
// bridge (§43).
//
// Complements `devcontainerExecHandler.ts`'s one-shot exec with a persistent
// interactive session: spawns `devcontainer exec ... -- /bin/sh` via the
// provisioner's resolved CLI + the injected `spawn`, then tunnels
// stdin/stdout/stderr over the WS events this module drives. Deliberately
// vscode-free — the same DI discipline as `devcontainerExecHandler.ts` — so it
// is a pure contract testable without a VS Code host.
//
// No node-pty: a native module is a packaging and supply-chain cost this
// session tunnel does not justify (CLAUDE.md §9), and the sentinel-marker
// command-boundary protocol the backend's SandboxSession already speaks
// (`core.pty_session` / `core.command_boundary`) gives command framing without
// a real TTY. Declared consequence (logged as its own backlog entry): no job
// control, no `isatty()`, and `interrupt` sends a best-effort signal to the
// child rather than a Ctrl-C to a real foreground process group.
//
// Backpressure: `server_devcontainer_session_flow` pauses/resumes BOTH of the
// child's stdout and stderr streams together, since the wire contract merges
// them into one interleaved byte stream (chunk_b64 carries no stream field) —
// matching SandboxSession.stream()'s single AsyncIterator[bytes] contract,
// which a real PTY would give for free but two separate Node pipes do not.

import type { ChildProcess } from 'child_process';
import type { SpawnFn } from './devcontainerProvisioner';

/** The subset of the provisioner this handler needs: how to invoke the CLI. */
export interface DevcontainerCliResolver {
    resolveCli(): { command: string; baseArgs: string[] };
}

export interface DevcontainerSessionHandlerDeps {
    provisioner: DevcontainerCliResolver;
    spawn: SpawnFn;
    /**
     * Absolute path of the workspace folder, or undefined when none is open.
     * A GETTER (not a static value) because this handler is stateful and
     * long-lived — unlike the one-shot `devcontainerExecHandler`, which is
     * handed a fresh value on every call, a session can outlive a workspace
     * change, so each open must re-resolve the current root rather than
     * close over a value captured at construction time.
     */
    workspaceRoot: () => string | undefined;
    /** Sends a client→backend event over the WS channel. */
    send: (message: { event_type: string; data: unknown }) => void;
    /** Host environment the whitelisted `env_keys` are resolved against. */
    env: NodeJS.ProcessEnv;
    log?: (message: string) => void;
    /** Idle ceiling (ms) before a session with no stdin/flow activity self-terminates. */
    idleCeilingMs?: number;
}

// Mirrors devcontainerProvisioner.ts's own SIGTERM→SIGKILL escalation window.
const KILL_GRACE_MS = 3000;
// No stdin/flow frame within this budget means the backend that opened this
// session has evaporated (crashed, disconnected, or otherwise never sent a
// close) — self-terminate rather than leak the child indefinitely. Deliberately
// keyed on INBOUND control frames only, not outbound data: a session whose
// shell is still chattering but whose backend has gone silent is exactly the
// orphan this ceiling exists to reap.
const IDLE_CEILING_MS = 10 * 60 * 1000;
// Same coalescing discipline as devcontainerExecHandler.ts's DEBT-083 fix —
// batch rapid child `data` events into a handful of WS frames.
const STREAM_COALESCE_MS = 50;
const STREAM_CHUNK_CAP_BYTES = 8192;

interface HostSession {
    sessionId: string;
    child: ChildProcess;
    buf: Buffer[];
    bufBytes: number;
    flushTimer: ReturnType<typeof setTimeout> | null;
    idleTimer: ReturnType<typeof setTimeout> | null;
    killTimer: ReturnType<typeof setTimeout> | null;
    closed: boolean;
}

interface SessionOpenData {
    session_id: string; session_ref: string; cwd: string; env_keys?: string[];
}
interface SessionStdinData {
    session_id: string; session_ref: string; data_b64: string;
}
interface SessionSignalData {
    session_id: string; session_ref: string; signal: 'interrupt' | 'kill';
}
interface SessionFlowData {
    session_id: string; session_ref: string; paused: boolean;
}
interface SessionCloseData {
    session_id: string; session_ref: string;
}

/**
 * Owns every live interactive devcontainer session for one host process.
 * Stateful by necessity (unlike the one-shot `devcontainerExecHandler`'s pure
 * function): a session's child process, coalescing buffer, and idle timer all
 * survive across many separate inbound WS frames.
 */
export class DevcontainerSessionHandler {
    private readonly sessions = new Map<string, HostSession>();

    constructor(private readonly deps: DevcontainerSessionHandlerDeps) {}

    /**
     * Handle one server→host §43 event. Returns true when the message was one
     * of ours (and was handled), false otherwise so the caller can keep routing.
     */
    handleServerEvent(msg: { event_type?: string; data?: unknown }): boolean {
        switch (msg.event_type) {
            case 'server_devcontainer_session_open':
                this.handleOpen(msg.data as SessionOpenData);
                return true;
            case 'server_devcontainer_session_stdin':
                this.handleStdin(msg.data as SessionStdinData);
                return true;
            case 'server_devcontainer_session_signal':
                this.handleSignal(msg.data as SessionSignalData);
                return true;
            case 'server_devcontainer_session_flow':
                this.handleFlow(msg.data as SessionFlowData);
                return true;
            case 'server_devcontainer_session_close':
                this.handleClose(msg.data as SessionCloseData);
                return true;
            default:
                return false;
        }
    }

    /** How many sessions are currently tracked (tests / diagnostics only). */
    get sessionCount(): number {
        return this.sessions.size;
    }

    /**
     * Kill every tracked child and clear all timers. Idempotent. Called from
     * the extension's dispose lifecycle (mirrors `disposeDevcontainerProvisioner`)
     * so a window reload or extension deactivation can never orphan a driver.
     */
    dispose(): void {
        for (const session of this.sessions.values()) {
            this.clearTimers(session);
            this.killChild(session);
        }
        this.sessions.clear();
    }

    // ── handlers ─────────────────────────────────────────────────────────────

    private handleOpen(data: SessionOpenData): void {
        const { session_id, session_ref } = data;
        const existing = this.sessions.get(session_ref);
        if (existing) {
            // Idempotent re-open of a live session_ref: report success against
            // the existing handle rather than spawning a second child.
            this.deps.send({
                event_type: 'client_devcontainer_session_opened',
                data: { session_id, session_ref, ok: true },
            });
            return;
        }
        const workspaceRoot = this.deps.workspaceRoot();
        if (!workspaceRoot) {
            this.deps.send({
                event_type: 'client_devcontainer_session_opened',
                data: { session_id, session_ref, ok: false, detail: 'no workspace folder open' },
            });
            return;
        }
        // Resolve allowlisted NAMES to values from the host environment —
        // mirrors devcontainerExecHandler.ts's own secret-hygiene discipline.
        const env: NodeJS.ProcessEnv = {};
        for (const key of data.env_keys ?? []) {
            const value = this.deps.env[key];
            if (value !== undefined) { env[key] = value; }
        }

        const cli = this.deps.provisioner.resolveCli();
        let child: ChildProcess;
        try {
            child = this.deps.spawn(
                cli.command,
                [...cli.baseArgs, 'exec', '--workspace-folder', workspaceRoot, '--', '/bin/sh'],
                { cwd: workspaceRoot, env },
            );
        } catch (err) {
            this.deps.log?.(`devcontainer session spawn failed: ${errText(err)}`);
            this.deps.send({
                event_type: 'client_devcontainer_session_opened',
                data: { session_id, session_ref, ok: false, detail: errText(err) },
            });
            return;
        }

        const session: HostSession = {
            sessionId: session_id, child,
            buf: [], bufBytes: 0,
            flushTimer: null, idleTimer: null, killTimer: null,
            closed: false,
        };
        this.sessions.set(session_ref, session);
        this.touch(session_ref, session);

        child.stdout?.on('data', (c: Buffer) => this.onData(session_ref, session, c));
        child.stderr?.on('data', (c: Buffer) => this.onData(session_ref, session, c));
        child.on('error', (err: Error) => {
            this.deps.log?.(`devcontainer session child error: ${err.message}`);
            this.finishSession(session_ref, session, -1);
        });
        child.on('close', (code: number | null) => {
            this.finishSession(session_ref, session, code ?? -1);
        });

        this.deps.send({
            event_type: 'client_devcontainer_session_opened',
            data: { session_id, session_ref, ok: true },
        });
    }

    private handleStdin(data: SessionStdinData): void {
        const session = this.sessions.get(data.session_ref);
        if (!session || session.closed) { return; }
        this.touch(data.session_ref, session);
        try {
            session.child.stdin?.write(Buffer.from(data.data_b64, 'base64'));
        } catch (err) {
            this.deps.log?.(`devcontainer session stdin write failed: ${errText(err)}`);
        }
    }

    private handleSignal(data: SessionSignalData): void {
        const session = this.sessions.get(data.session_ref);
        if (!session || session.closed) { return; }
        this.touch(data.session_ref, session);
        if (data.signal === 'kill') {
            this.killChild(session);
            return;
        }
        // No real TTY / process group here (the declared no-node-pty
        // tradeoff): a best-effort SIGINT to the child itself, not a true
        // Ctrl-C to a foreground process group.
        try { session.child.kill('SIGINT'); } catch { /* already dead */ }
    }

    private handleFlow(data: SessionFlowData): void {
        const session = this.sessions.get(data.session_ref);
        if (!session || session.closed) { return; }
        this.touch(data.session_ref, session);
        if (data.paused) {
            session.child.stdout?.pause();
            session.child.stderr?.pause();
        } else {
            session.child.stdout?.resume();
            session.child.stderr?.resume();
        }
    }

    private handleClose(data: SessionCloseData): void {
        const session = this.sessions.get(data.session_ref);
        if (!session) { return; } // unknown/already-closed session_ref — idempotent no-op
        this.killChild(session);
    }

    // ── internals ────────────────────────────────────────────────────────────

    private onData(session_ref: string, session: HostSession, chunk: Buffer): void {
        session.buf.push(chunk);
        session.bufBytes += chunk.length;
        if (session.bufBytes >= STREAM_CHUNK_CAP_BYTES) {
            this.flush(session_ref, session);
            return;
        }
        if (!session.flushTimer) {
            session.flushTimer = setTimeout(() => this.flush(session_ref, session), STREAM_COALESCE_MS);
        }
    }

    private flush(session_ref: string, session: HostSession): void {
        if (session.flushTimer) {
            clearTimeout(session.flushTimer);
            session.flushTimer = null;
        }
        if (session.buf.length === 0) { return; }
        const combined = Buffer.concat(session.buf);
        session.buf = [];
        session.bufBytes = 0;
        this.deps.send({
            event_type: 'client_devcontainer_session_stream',
            data: {
                session_id: session.sessionId, session_ref,
                chunk_b64: combined.toString('base64'),
            },
        });
    }

    private finishSession(session_ref: string, session: HostSession, exitCode: number): void {
        if (session.closed) { return; }
        session.closed = true;
        this.clearTimers(session);
        // Flush any coalesced residue BEFORE the exit frame — ordering matters
        // the same way it does for the one-shot exec bridge (DEBT-083): the
        // exit frame resolves the backend's demux consumer, and a stream chunk
        // arriving after it is dropped as an unknown/already-closed session.
        this.flush(session_ref, session);
        this.deps.send({
            event_type: 'client_devcontainer_session_exit',
            data: { session_id: session.sessionId, session_ref, exit_code: exitCode },
        });
        this.sessions.delete(session_ref);
    }

    /**
     * Request termination. Mirrors `DevcontainerProvisioner`'s own
     * `_killChild`: hard kill on Windows (no signal semantics), SIGTERM →
     * SIGKILL escalation elsewhere. This only requests termination — the
     * child's own `close` handler (`finishSession`) fires once the process
     * actually exits and does the bookkeeping / exit-frame emission, so
     * calling this twice on an already-closed session is a safe no-op.
     */
    private killChild(session: HostSession): void {
        if (session.closed) { return; }
        if (process.platform === 'win32') {
            try { session.child.kill(); } catch { /* already dead */ }
            return;
        }
        try { session.child.kill('SIGTERM'); } catch { /* already dead */ }
        session.killTimer = setTimeout(() => {
            try { session.child.kill('SIGKILL'); } catch { /* already dead */ }
        }, KILL_GRACE_MS);
        session.killTimer.unref?.();
    }

    private touch(session_ref: string, session: HostSession): void {
        if (session.idleTimer) {
            clearTimeout(session.idleTimer);
        }
        const budget = this.deps.idleCeilingMs ?? IDLE_CEILING_MS;
        session.idleTimer = setTimeout(() => {
            this.deps.log?.(
                `devcontainer session idle ceiling reached (session_ref=${session_ref}) — self-terminating`,
            );
            this.killChild(session);
        }, budget);
        session.idleTimer.unref?.();
    }

    private clearTimers(session: HostSession): void {
        if (session.flushTimer) { clearTimeout(session.flushTimer); session.flushTimer = null; }
        if (session.idleTimer) { clearTimeout(session.idleTimer); session.idleTimer = null; }
        if (session.killTimer) { clearTimeout(session.killTimer); session.killTimer = null; }
    }
}

function errText(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
}
