import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as cp from 'child_process';
import * as net from 'net';
import * as crypto from 'crypto';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { PatchActuator, type ApplyWorkspaceEditPayload, type PatchedFileDiff } from '../core/PatchActuator';
import { InlineMutationManager } from '../core/InlineMutationManager';
import { GrammarLexer } from '../core/GrammarLexer';
import { StreamingCodeTokenizer } from '../core/StreamingCodeTokenizer';
import { WSClient, WSMessageCallback, WSStatusCallback } from '../api/ws_client';
import { BudgetLimitMode, DreamingProfile, OrchestrationMode, WORKSPACE_STATE_KEYS } from '../shared/config';
import { APIClient } from '../api/api_client';
import type { AilienantConfig, Session } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { ConfigLoader } from '../shared/config_loader';
import { WorkspacePathIndex, extractMentions, FOLDER_EXPANSION_CAP, FOLDER_EXPANSION_GIVE_UP } from './workspacePathIndex';
import { HitlNotifier, type HITLApprovalRequestPayload, type HitlMode } from './hitlNotifier';

function findBackendPath(extensionFsPath: string): string | null {
    const candidates = [
        ...(vscode.workspace.workspaceFolders ?? []).map(
            f => path.join(f.uri.fsPath, 'ailienant-core')
        ),
        path.join(extensionFsPath, '..', 'ailienant-core'),
    ];
    for (const candidate of candidates) {
        if (fs.existsSync(path.join(candidate, 'main.py'))) {
            return candidate;
        }
    }
    return null;
}

function findVenvPython(backendPath: string): string {
    const candidates = [
        path.join(backendPath, '.venv', 'Scripts', 'python.exe'),  // dot-venv Windows
        path.join(backendPath, 'venv',  'Scripts', 'python.exe'),  // plain venv Windows
        path.join(backendPath, '.venv', 'bin', 'python'),          // dot-venv Unix
        path.join(backendPath, 'venv',  'bin', 'python'),          // plain venv Unix
    ];
    for (const p of candidates) {
        if (fs.existsSync(p)) { return p; }
    }
    return process.platform === 'win32' ? 'python' : 'python3';
}

export function findFreePort(): Promise<number> {
    return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.listen(0, '127.0.0.1', () => {
            const port = (server.address() as net.AddressInfo).port;
            server.close(() => resolve(port));
        });
        server.on('error', reject);
    });
}

type CoreState = 'stopped' | 'starting' | 'running' | 'crashed';

export class CoreProcessManager {
    private _proc: cp.ChildProcess | null = null;
    private _state: CoreState = 'stopped';
    private readonly _outputChannel: vscode.OutputChannel;
    private _crashRetries = 0;
    private static readonly MAX_RETRIES = 3;
    private static readonly RETRY_DELAY_MS = 2000;

    readonly port: number;
    readonly token: string;
    private readonly _extensionFsPath: string;

    constructor(port: number, token: string, extensionFsPath: string) {
        this.port = port;
        this.token = token;
        this._extensionFsPath = extensionFsPath;
        this._outputChannel = vscode.window.createOutputChannel('AILIENANT Core');
    }

    getState(): CoreState { return this._state; }

    async start(): Promise<void> {
        if (this._state === 'starting' || this._state === 'running') { return; }
        this._state = 'starting';

        const backendPath = findBackendPath(this._extensionFsPath);
        if (!backendPath) {
            this._state = 'crashed';
            this._outputChannel.appendLine('[AILIENANT] Cannot locate ailienant-core/. Open the monorepo root as workspace or set ailienant.coreStartCommand.');
            void vscode.window.showWarningMessage(
                'AILIENANT: Cannot locate ailienant-core/.',
                'Show Output', 'Open Settings',
            ).then((choice) => {
                if (choice === 'Show Output') { this._outputChannel.show(); }
                if (choice === 'Open Settings') {
                    void vscode.commands.executeCommand('workbench.action.openSettings', 'ailienant.coreStartCommand');
                }
            });
            return;
        }

        const python = findVenvPython(backendPath);
        const env = {
            ...process.env,
            AILIENANT_API_PORT: String(this.port),
            AILIENANT_AUTH_TOKEN: this.token,
        };

        this._outputChannel.appendLine(`[AILIENANT] Starting Core on 127.0.0.1:${this.port} ...`);

        const proc = cp.spawn(
            python,
            ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(this.port)],
            { cwd: backendPath, env, detached: false },
        );

        proc.stdout?.on('data', (chunk: Buffer) => this._outputChannel.append(chunk.toString()));
        proc.stderr?.on('data', (chunk: Buffer) => this._outputChannel.append(chunk.toString()));

        proc.on('error', (err) => {
            this._outputChannel.appendLine(`[AILIENANT] Failed to start: ${err.message}`);
            this._state = 'crashed';
        });

        proc.on('close', (code) => {
            this._proc = null;
            if (this._state === 'stopped') { return; }  // intentional stop — no retry
            if (this._crashRetries < CoreProcessManager.MAX_RETRIES) {
                this._crashRetries++;
                this._outputChannel.appendLine(
                    `[AILIENANT] Process exited (code ${code}). Retrying in ${CoreProcessManager.RETRY_DELAY_MS}ms (${this._crashRetries}/${CoreProcessManager.MAX_RETRIES})...`
                );
                this._state = 'starting';
                setTimeout(() => { void this.start(); }, CoreProcessManager.RETRY_DELAY_MS);
            } else {
                this._state = 'crashed';
                this._outputChannel.appendLine(`[AILIENANT] Core stopped (code ${code}). Max retries reached.`);
                void vscode.window.showErrorMessage(
                    'AILIENANT: Core process stopped unexpectedly.', 'Show Output'
                ).then((choice) => {
                    if (choice === 'Show Output') { this._outputChannel.show(); }
                });
            }
        });

        this._proc = proc;
        this._state = 'running';
    }

    async stop(): Promise<void> {
        if (!this._proc) { this._state = 'stopped'; return; }
        const proc = this._proc;
        this._proc = null;
        this._state = 'stopped';  // prevents close handler from auto-retrying
        return new Promise<void>((resolve) => {
            proc.once('close', () => resolve());
            if (process.platform === 'win32') {
                proc.kill();
            } else {
                proc.kill('SIGTERM');
                setTimeout(() => {
                    try { proc.kill('SIGKILL'); } catch { /* already dead */ }
                }, 3000);
            }
        });
    }

    async restart(): Promise<void> {
        this._crashRetries = 0;
        await this.stop();
        await this.start();
    }

    dispose(): void {
        void this.stop();
        this._outputChannel.dispose();
    }
}

// Generate a 256-bit ephemeral auth token (64-char hex). Called once per activate().
export function generateAuthToken(): string {
    return crypto.randomBytes(32).toString('hex');
}

interface TitleUpdater {
    (sessionId: string, title: string): void;
}

// Phase 7.9.B.20 — per-session chat transcript persistence (survives VS Code close).
const TRANSCRIPT_KEY_PREFIX = 'ailienant.transcript.';
const MAX_PERSISTED_MESSAGES = 200;  // bound storage growth per session

// Phase 7.11.6 — Rich Tool Chips: persisted alongside the transcript so chips
// (including their final status, output, and dep_graph) survive a panel close.
import type { ToolCallShape, PlanDocumentShape, PlanWBSStep } from '../shared/config';
interface StoredMessage {
    id?: string;   // Phase 7.12 — stable turn id; keys the REHYDRATE_TRANSCRIPT merge.
    role: 'user' | 'assistant';
    content: string;
    steps?: string[];
    stepsDone?: boolean;
    toolCalls?: ToolCallShape[];
    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel checkpoint metadata. Survives
    // PERSIST_TRANSCRIPT so the per-message ↪ Branch button still works on
    // sessions reopened after a VS Code restart.
    checkpoint_id?: string;
    is_abort_savepoint?: boolean;
    // Execution checklist — durable audit evidence of which WBS steps the agent
    // completed; survives a reload so the record is never lost.
    checklist?: PlanWBSStep[];
}
interface StoredNattMessage { id?: string; role: 'natt' | 'user'; content: string; }
interface StoredTranscript { messages: StoredMessage[]; nattMessages: StoredNattMessage[]; }

export class WorkspacePanelManager {
    // One panel per AILIENANT session (session.id → panel)
    private _panels: Map<string, vscode.WebviewPanel> = new Map();
    private _sessions: Map<string, Session> = new Map();
    // Track which sessions currently have a task in-flight (for conflict warnings)
    private _runningTasks: Set<string> = new Set();
    // Track which sessions have the Natt pane open (gates native notifications)
    private _nattOpen: Set<string> = new Set();
    // Latest finalized plan per session, held in HOST memory (not webview
    // setState) so a large MissionSpecification can never blow the webview's
    // persistent-state quota. Re-posted when a torn-down panel becomes visible.
    private _latestPlan: Map<string, PlanDocumentShape> = new Map();
    // Per-session streaming code tokenizer (host-push incremental AST).
    // Keyed by session.id; reset on stream-end and on WS disconnect.
    private _streamTokenizers: Map<string, StreamingCodeTokenizer> = new Map();
    private readonly _configLoader: ConfigLoader;
    private readonly _disposables: vscode.Disposable[] = [];
    private _onTitleUpdate: TitleUpdater | undefined;
    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: invoked when the backend
    // broadcasts `server_session_branched`. Implementation lives in
    // extension.ts so workspace_panel.ts stays decoupled from the sidebar.
    private _onSessionBranched: ((session: Session) => void) | undefined;
    // Phase 7.9.A.5.1 — managed child process; set from activate() via setCoreManager().
    private _coreManager: CoreProcessManager | null = null;
    // Phase 7.11.4 — host-side workspace path trie for @mention autocomplete.
    // Lazily bootstrapped on first WORKSPACE_PATHS_QUERY / SUBMIT_TASK to
    // avoid a startup-time `findFiles` on workspaces the user never queries.
    private _pathIndex: WorkspacePathIndex | null = null;
    private _pathIndexBootstrap: Promise<void> | null = null;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _workspaceState: vscode.Memento,
    ) {
        this._configLoader = new ConfigLoader();
        this._disposables.push(this._configLoader);

        // Broadcast config changes to every open panel
        this._configLoader.onChange((cfg) => {
            for (const panel of this._panels.values()) {
                panel.webview.postMessage({ type: 'CONFIG_UPDATED', config: cfg });
            }
        });

        // Broadcast workspace folder changes to every open panel
        this._disposables.push(
            vscode.workspace.onDidChangeWorkspaceFolders(() => {
                const folder = vscode.workspace.workspaceFolders?.[0]?.name ?? '';
                for (const panel of this._panels.values()) {
                    panel.webview.postMessage({ type: 'WORKSPACE_UPDATED', workspaceFolder: folder });
                }
            })
        );

        // Connection-level events are not owned by any single session (the backend
        // addresses them to the physical connection, not a panel): Cmd+K inline
        // edits run in the host editor via InlineMutationManager (handled once),
        // and other workspace-global events (e.g. indexing progress) are mirrored
        // to every open panel. Session-tagged events bypass this entirely — they
        // are demultiplexed straight to their owning panel's per-session handler.
        WSClient.getInstance().onMessageGlobal((raw) => {
            const m = raw as { event_type?: string; data?: unknown };
            if (!m.event_type) { return; }
            if (
                m.event_type === 'server_inline_edit_start' ||
                m.event_type === 'server_inline_edit_delta' ||
                m.event_type === 'server_inline_edit_end'
            ) {
                InlineMutationManager.instance.handle(m.event_type, m.data);
                return;
            }
            for (const panel of this._panels.values()) {
                panel.webview.postMessage({ type: m.event_type, payload: m.data });
            }
        });
    }

    public setTitleUpdater(updater: TitleUpdater): void {
        this._onTitleUpdate = updater;
    }

    /** Phase 7.11.8 — extension.ts injects a callback that persists the new
     *  branched session in the sidebar and opens it. Decouples this manager
     *  from the SessionBrowser. */
    public setSessionBranchedHandler(handler: (session: Session) => void): void {
        this._onSessionBranched = handler;
    }

    public setCoreManager(manager: CoreProcessManager): void {
        this._coreManager = manager;
    }

    /**
     * Phase 7.11.4 — lazily bootstrap the workspace path index. Returns the
     * trie once the initial `findFiles` scan has resolved; subsequent calls
     * return the same instance immediately.
     */
    private async _getPathIndex(): Promise<WorkspacePathIndex> {
        if (this._pathIndex === null) {
            this._pathIndex = new WorkspacePathIndex();
            this._pathIndexBootstrap = this._pathIndex.bootstrap();
        }
        if (this._pathIndexBootstrap !== null) {
            await this._pathIndexBootstrap;
        }
        return this._pathIndex;
    }

    public dispose(): void {
        this._coreManager?.dispose();
        this._pathIndex?.dispose();
        this._pathIndex = null;
        for (const d of this._disposables) { d.dispose(); }
        for (const panel of this._panels.values()) { panel.dispose(); }
    }

    /** Close and dispose the panel for a deleted session (no-op if not open). */
    public closeSession(id: string): void {
        const panel = this._panels.get(id);
        if (panel) { panel.dispose(); }
    }

    // ── Phase 7.9.B.20 — transcript persistence (host-side, per session.id) ──
    private _getTranscript(sessionId: string): StoredTranscript {
        return this._workspaceState.get<StoredTranscript>(
            TRANSCRIPT_KEY_PREFIX + sessionId,
            { messages: [], nattMessages: [] },
        );
    }

    private _saveTranscript(sessionId: string, t: StoredTranscript): void {
        void this._workspaceState.update(TRANSCRIPT_KEY_PREFIX + sessionId, {
            messages: t.messages.slice(-MAX_PERSISTED_MESSAGES),
            nattMessages: t.nattMessages.slice(-MAX_PERSISTED_MESSAGES),
        });
    }

    /** Drop a deleted session's persisted transcript. */
    public clearTranscript(sessionId: string): void {
        void this._workspaceState.update(TRANSCRIPT_KEY_PREFIX + sessionId, undefined);
    }

    /**
     * Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: mint + open a new branched
     * session after the backend broadcasts `server_session_branched`.
     *
     * 1. Read the parent's persisted transcript.
     * 2. Slice it at the message whose checkpoint_id matches the fork target
     *    (purely cosmetic: the backend's L2 row is the canonical state).
     * 3. Build a Session linked to the parent via parent_thread_id +
     *    parent_checkpoint_id (provenance — never carries any graph state).
     * 4. Save the new transcript + notify the sidebar callback so the new
     *    Session lands in the SessionBrowser and gets opened.
     */
    private _handleSessionBranched(
        parent: Session,
        newSessionId: string,
        fromCheckpointId: string,
    ): void {
        const parentTx = this._getTranscript(parent.id);
        const cutIdx = parentTx.messages.findIndex(m => m.checkpoint_id === fromCheckpointId);
        const seed: StoredMessage[] = cutIdx >= 0
            ? parentTx.messages.slice(0, cutIdx + 1)
            : parentTx.messages.slice();

        const now = new Date().toISOString();
        const newSession: Session = {
            id: newSessionId,
            title: `↪ Branch of ${parent.title || 'session'}`,
            created_at: now,
            last_modified: now,
            message_count: seed.length,
            model_tier: parent.model_tier,
            thread_id: newSessionId,
            parent_thread_id: parent.id,
            parent_checkpoint_id: fromCheckpointId,
        };

        this._saveTranscript(newSessionId, {
            messages: seed,
            nattMessages: parentTx.nattMessages.slice(),
        });

        const handler = this._onSessionBranched;
        if (handler) {
            handler(newSession);
        }
    }

    /** Reveal existing panel or open a new one for the given session. */
    public openSession(session: Session): void {
        const existing = this._panels.get(session.id);
        if (existing) {
            existing.reveal(vscode.ViewColumn.One);
            return;
        }
        this._createPanel(session);
    }

    private _nattName(): string {
        return this._configLoader.current?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;
    }

    /**
     * Phase 7.9.A.5.1 — Health-aware activation, run once per session open.
     * If the Core is already healthy, connect immediately. Otherwise poll while
     * CoreProcessManager starts it (~30 s budget). Falls back gracefully if no
     * manager is configured (e.g. manual external backend).
     */
    private async _ensureBackend(sessionId: string): Promise<void> {
        const api = APIClient.getInstance();
        if (await api.checkHealth()) {
            SessionManager.forSession(sessionId).ensureConnected();
            return;
        }
        if (!this._coreManager) { return; }
        for (let i = 0; i < 30; i++) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            if (await api.checkHealth()) {
                SessionManager.forSession(sessionId).ensureConnected();
                return;
            }
        }
    }

    private _createPanel(session: Session): void {
        const tabTitle = session.title.trim() || 'AILIENANT';
        const panel = vscode.window.createWebviewPanel(
            'ailienant.workspace',
            tabTitle,
            vscode.ViewColumn.One,
            {
                enableScripts: true,
                // Phase 7.11.2 (ADR-706 §4.5c) — webview state survives tab-switch via
                // acquireVsCodeApi().setState/getState (see workspaceStore), so the DOM
                // itself no longer needs to be kept in memory between hides.
                retainContextWhenHidden: false,
                localResourceRoots: [
                    vscode.Uri.joinPath(this._extensionUri, 'dist'),
                    vscode.Uri.joinPath(this._extensionUri, 'media'),
                ],
            },
        );
        panel.iconPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'icon-color.svg');

        // Phase 7.11.7 (ADR-706 §4.5f) — native HITL push-notification bridge.
        // Fires a VS Code OS toast when an approval lands while this panel is
        // hidden; Approve/Reject post back through the existing client_hitl_response
        // event. The in-chat rich card remains the primary surface when visible.
        const hitlNotifier = new HitlNotifier({
            windowApi: vscode.window,
            getMode: () => (vscode.workspace.getConfiguration('ailienant.notifications')
                .get<string>('hitlNativeMode') ?? 'auto') as HitlMode,
            send: (approvalId, approved) => {
                WSClient.getInstance().send({
                    event_type: 'client_hitl_response',
                    data: { approval_id: approvalId, approved },
                });
            },
            revealPanel: () => {
                panel.reveal(vscode.ViewColumn.One);
                panel.webview.postMessage({ type: 'FOCUS_HITL_CARD' });
            },
        });
        hitlNotifier.setVisibility(panel.visible);
        panel.onDidChangeViewState(e => {
            hitlNotifier.setVisibility(e.webviewPanel.visible);
            // Phase 7.12 — when a hidden panel becomes visible again, the webview
            // may have been torn down (retainContextWhenHidden:false) and reloaded
            // from a stale creation-time data-initial snapshot. Re-post the
            // authoritative host transcript; the webview merges it by message id.
            if (e.webviewPanel.visible) {
                const t = this._getTranscript(session.id);
                e.webviewPanel.webview.postMessage({
                    type: 'REHYDRATE_TRANSCRIPT',
                    messages: t.messages,
                    nattMessages: t.nattMessages,
                });
                // Phase 7.12.9 (Fix 1) — the singleton WS survives the webview
                // teardown, but a remounted webview boots with a stale status and
                // the socket may have dropped while hidden. Re-assert the tunnel
                // and mirror the *actual* socket state back to the indicator.
                SessionManager.forSession(session.id).ensureConnected();
                e.webviewPanel.webview.postMessage({
                    type: 'WS_STATUS',
                    payload: WSClient.getInstance().getStatus(),
                });
                // Re-post the last finalized plan from host memory — the
                // remounted webview holds it only in transient React state, so
                // without this the Plan panel would be empty after a tab-switch.
                // BUT: only re-post if its summary pointer is not already in the
                // persisted transcript. Match the plan's OWN summary text, not a
                // fixed phrase — the pointer reads "Drafted a plan…" on the plan
                // surface but "Proposed N file change(s)…" in Ask/Auto, and a
                // phrase-specific guard let the latter re-inject a duplicate
                // bubble on every reveal. (The webview handler is also idempotent
                // on the summary; this is defense in depth.)
                const plan = this._latestPlan.get(session.id);
                if (plan) {
                    const t = this._getTranscript(session.id);
                    const summaryHead = (plan.summary ?? '').split('\n')[0].trim();
                    const hasInTranscript = summaryHead.length > 0 && t.messages.some(m =>
                        m.role === 'assistant' && (m.content ?? '').includes(summaryHead)
                    );
                    if (!hasInTranscript) {
                        e.webviewPanel.webview.postMessage({ type: 'server_plan_document', payload: plan });
                    }
                }
            }
        });
        panel.onDidDispose(() => hitlNotifier.setVisibility(false));

        panel.webview.html = this._renderHtml(panel.webview, session);

        // ── WS status → forward to this panel ──────────────────────────────
        // Phase 7.9.B.20 — on (re)connect, re-seed the backend's short-term memory
        // from this session's persisted transcript so a reopened session keeps
        // conversational continuity. Seed-if-absent on the server; sent once per
        // connection so an in-flight conversation is never clobbered.
        let _historyRestored = false;
        const wsStatusHandler: WSStatusCallback = (status) => {
            panel.webview.postMessage({ type: 'WS_STATUS', payload: status });
            // Memory-leak guard: if the WS drops without a stream-end event,
            // the tokenizer's pending buffer and in-flight promise must be
            // abandoned — otherwise they accumulate as zombie state.
            if (status === 'disconnected') {
                this._streamTokenizers.get(session.id)?.reset();
            }
            if (status === 'connected' && !_historyRestored) {
                const stored = this._getTranscript(session.id).messages;
                if (stored.length > 0) {
                    WSClient.getInstance().send({
                        event_type: 'client_restore_history',
                        data: { messages: stored.map(m => ({ role: m.role, content: m.content })) },
                    });
                }
                _historyRestored = true;
            }
        };
        WSClient.getInstance().onStatus(wsStatusHandler);

        // ── WS messages → route to this panel only ──────────────────────────
        // Events with a matching session_id go here; events without one broadcast.
        const wsMsgHandler: WSMessageCallback = (raw) => {
            const msg = raw as { event_type?: string; data?: unknown };
            if (!msg.event_type) { return; }
            const data = msg.data as Record<string, unknown> | undefined;
            if (data?.session_id && data.session_id !== session.id) { return; }

            // Enterprise Write Pipeline: the host (not the webview)
            // actuates approved patches via vscode.workspace.applyEdit, then acks.
            if (msg.event_type === 'server_apply_workspace_edit') {
                void PatchActuator.apply(msg.data as ApplyWorkspaceEditPayload).then(async (result) => {
                    WSClient.getInstance().send({ event_type: 'client_patch_applied', data: result });
                    if (result.ok && result.diffs && result.diffs.length > 0) {
                        await GrammarLexer.enrich(result.diffs);  // best-effort; never throws
                        panel.webview.postMessage({
                            type: 'RENDER_DIFF',
                            payload: { patch_id: result.patch_id, files: result.diffs },
                        });
                    }
                });
                return;
            }

            // Cache the finalized plan in host memory so it survives a webview
            // teardown without ever touching the webview's persistent-state quota.
            if (msg.event_type === 'server_plan_document' && msg.data) {
                this._latestPlan.set(session.id, msg.data as unknown as PlanDocumentShape);
            }

            // Streaming code tokenizer: feed chat token chunks to the host-side
            // fence state machine. When it completes a code line it posts
            // STREAM_CODE_TOKENS to the webview, which paints it immediately —
            // before stream-end and before the full-block CODE_TOKENS round-trip.
            if (msg.event_type === 'server_token_chunk') {
                const chunk = (msg.data as { token?: string } | undefined)?.token ?? '';
                if (chunk) {
                    let tokenizer = this._streamTokenizers.get(session.id);
                    if (!tokenizer) {
                        tokenizer = new StreamingCodeTokenizer(
                            (langHint) => GrammarLexer.createLineTokenizer(langHint),
                            (emit) => {
                                panel.webview.postMessage({ type: 'STREAM_CODE_TOKENS', payload: emit });
                            },
                        );
                        this._streamTokenizers.set(session.id, tokenizer);
                    }
                    tokenizer.push(chunk);
                }
            }

            // A FILE_WRITE approval rides its proposed diff in the same payload so
            // the inline diff and the Accept/Reject row mount atomically on the
            // client — never two racing messages. Enrich here (the grammar engine
            // is host-only) and forward ONE combined message; the native toast
            // still fires for the hidden-panel case. Non-write approvals (e.g.
            // BUDGET_OVERFLOW) carry no proposed_files and fall through to the
            // plain forward below.
            if (msg.event_type === 'server_hitl_approval_request') {
                const reqData = msg.data as HITLApprovalRequestPayload & {
                    proposed_files?: Array<{ file_path: string; new_content: string; base_hash?: string | null }>;
                };
                hitlNotifier.onApprovalRequest(reqData);
                const proposed = reqData.proposed_files;
                if (proposed && proposed.length > 0) {
                    void (async () => {
                        // The approval MUST reach the webview even if the preview
                        // fails — losing it here strands the backend on its 300 s
                        // wait. So build the diff best-effort, but always post
                        // exactly one message: with `files` when the preview
                        // succeeds, bare otherwise (reqData still carries
                        // proposed_files, from which the webview synthesizes a
                        // degraded diff). One message either way — no race.
                        let diffs: PatchedFileDiff[] = [];
                        try {
                            diffs = await PatchActuator.preview(
                                proposed.map(f => ({
                                    file_path: f.file_path,
                                    new_content: f.new_content,
                                    base_hash: f.base_hash,
                                })),
                            );
                            await GrammarLexer.enrich(diffs);  // best-effort; never throws
                        } catch (err) {
                            console.error('[AILIENANT] HITL preview failed — forwarding bare approval', err);
                            diffs = [];
                        }
                        panel.webview.postMessage({
                            type: 'server_hitl_approval_request',
                            payload: diffs.length > 0 ? { ...reqData, files: diffs } : reqData,
                        });
                    })();
                    return;
                }
            }

            panel.webview.postMessage({ type: msg.event_type, payload: msg.data });
            this._maybeFireCriticalNotif(msg, session.id, panel);
            // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: a new session was
            // minted from a branch op. The backend broadcasts to BOTH the
            // parent thread AND the new thread; we process only when this
            // panel IS the parent so we don't double-create.
            if (msg.event_type === 'server_session_branched') {
                const d = msg.data as {
                    parent_session_id: string;
                    new_session_id: string;
                    from_checkpoint_id: string;
                };
                if (d.parent_session_id === session.id) {
                    this._handleSessionBranched(session, d.new_session_id, d.from_checkpoint_id);
                }
            }
            // Clear running-task marker and streaming tokenizer on stream/task completion.
            // Resetting the tokenizer on stream-end is the normal-path cleanup;
            // the disconnect path handles abrupt drops (see wsStatusHandler above).
            if (
                msg.event_type === 'server_stream_end' ||
                msg.event_type === 'server_task_complete'
            ) {
                this._runningTasks.delete(session.id);
                this._streamTokenizers.get(session.id)?.reset();
                // Refresh the context-budget meter once the turn settles: the
                // window has stopped growing, so this is the cheapest moment to
                // read its true occupancy. Fire-and-forget — a failed/empty read
                // just leaves the prior meter value (the fetch is null-safe).
                void APIClient.getInstance().fetchContextOccupancy(session.id).then((occ) => {
                    if (occ) {
                        panel.webview.postMessage({ type: 'CONTEXT_OCCUPANCY', payload: occ });
                    }
                });
            }
        };
        WSClient.getInstance().onMessage(session.id, wsMsgHandler);

        // ── Health-aware activation: connect now or auto-start the Core ──────
        void this._ensureBackend(session.id);

        // ── Panel → extension host messages ────────────────────────────────
        panel.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    const taskText = data.value as string;
                    this._maybeAutoTitle(session, taskText, panel);
                    // Inform this session if others are running (educational, non-blocking)
                    const parallelCount = [...this._runningTasks].filter(id => id !== session.id).length;
                    if (parallelCount > 0) {
                        panel.webview.postMessage({
                            type: 'PARALLEL_SESSION_NOTIFY',
                            count: parallelCount,
                        });
                    }
                    this._runningTasks.add(session.id);
                    // Refresh the context-budget meter at task start so it reflects
                    // the window the turn begins from — paired with the post-turn
                    // read on stream end, the meter updates each turn instead of
                    // only once. Fire-and-forget; a null read leaves the prior value.
                    void APIClient.getInstance().fetchContextOccupancy(session.id).then((occ) => {
                        if (occ) {
                            panel.webview.postMessage({ type: 'CONTEXT_OCCUPANCY', payload: occ });
                        }
                    });
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(taskText, activeDoc);
                    if (!intercepted) {
                        // Phase 7.11.4 — extract @file: / @folder: tokens and feed
                        // them through TaskPayload.explicit_mentions so the backend
                        // researcher's RAG-bypass path fires (agents/researcher.py:78).
                        let explicit_mentions: string[] | undefined;
                        if (/@(file|folder):/.test(taskText)) {
                            const idx = await this._getPathIndex();
                            const mentions = extractMentions(taskText, idx, (folder) => {
                                // Surface in the panel (where the user's eyes
                                // are) rather than a native popup outside it.
                                panel.webview.postMessage({
                                    type: 'MENTION_NOTIFY',
                                    level: 'warn',
                                    message: `@folder:${folder} too large (>${FOLDER_EXPANSION_GIVE_UP} files) — skipped. ` +
                                        `Use @file: for specific paths or narrow the folder.`,
                                });
                            });
                            if (mentions.length > 0) {
                                explicit_mentions = mentions;
                                const fileCount = mentions.length;
                                const cap = FOLDER_EXPANSION_CAP;
                                if (fileCount >= cap) {
                                    panel.webview.postMessage({
                                        type: 'MENTION_NOTIFY',
                                        level: 'info',
                                        message: `Capped @folder expansion at ${cap} files.`,
                                    });
                                }
                            }
                        }
                        const watchdogMs = await SessionManager.forSession(session.id).startAITask(taskText, {
                            explicit_mentions,
                            // Phase 9 (ADR-707) — forwarded from the Webview's
                            // persisted Native Thinking toggle (default true).
                            enable_native_thinking: data.enable_native_thinking as boolean | undefined,
                            // Forwarded from the Planner surface — routes this turn into
                            // the backend Socratic ideation loop when set.
                            planner_mode_active: data.planner_mode_active as boolean | undefined,
                            // The three-way mode selector governs the backend session
                            // permission policy (automatic | ask_before_edits | plan_mode).
                            execution_mode: data.execution_mode as string | undefined,
                            // Explicit skill the user invoked for this turn (snake_case).
                            invoked_skill_id: data.invoked_skill_id as string | undefined,
                        });
                        // Backend-governed stream-stall timeout (longer for slow local
                        // engines). The webview arms its watchdog from this — never a
                        // hardcoded UI constant.
                        if (typeof watchdogMs === 'number') {
                            panel.webview.postMessage({ type: 'STREAM_WATCHDOG_MS', payload: watchdogMs });
                        }
                    }
                    break;
                }
                case 'WORKSPACE_PATHS_QUERY': {
                    // Phase 7.11.4 — fast autocomplete from the host-side trie.
                    const prefix = String(data.prefix ?? '');
                    const idx = await this._getPathIndex();
                    const results = idx.query(prefix, 12);
                    panel.webview.postMessage({ type: 'WORKSPACE_PATHS_RESULT', results });
                    break;
                }
                case 'OPEN_CONTEXT_TERMINAL': {
                    // Phase 7.11.4 — @terminal stub: open the existing ContextOverlay
                    // terminal tab (no VS Code API exposes terminal output, see plan W…).
                    panel.webview.postMessage({ type: 'OPEN_CONTEXT', tab: 'terminal' });
                    break;
                }
                case 'ABORT_TASK':
                    this._runningTasks.delete(session.id);
                    SessionManager.forSession(session.id).abortCurrentTask();
                    break;
                case 'ABORT_MESH': {
                    // Phase 7.11.3 (ADR-706 §4.5b) — priority WS event that the
                    // backend resolves to asyncio.Task.cancel() on the running
                    // generation runner. Lives alongside ABORT_TASK (which only
                    // cancels the client-side HTTP fetch — useful for any other
                    // in-flight HTTP, harmless here).
                    const ws = WSClient.getInstance();
                    if (ws.getStatus() !== 'connected') {
                        // Socket is down — the abort can't reach the backend. Synthesize
                        // a negative ACK so the UI clears its optimistic isAborting flag
                        // and surfaces the failure, instead of freezing the Stop button.
                        panel.webview.postMessage({
                            type: 'server_abort_ack',
                            payload: { session_id: session.id, signalled: false },
                        });
                        break;
                    }
                    ws.send({
                        event_type: 'client_abort_mesh',
                        data: { session_id: session.id },
                    });
                    break;
                }
                case 'PTY_STDIN': {
                    // Interactive terminal: relay a line of stdin onto the WS so the
                    // backend feeds it to the session's live PTY. Droppable if the
                    // socket is down — a missed keystroke is not worth a queue.
                    const ws = WSClient.getInstance();
                    if (ws.getStatus() !== 'connected') { break; }
                    ws.send({
                        event_type: 'client_pty_write',
                        data: { session_id: session.id, data: data.data as string },
                    });
                    break;
                }
                case 'RETRY_TOOL':
                    // Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips: exact-replay
                    // retry. The backend looks up the stored ToolCallSpec keyed
                    // by (session_id, tool_call_id) and re-invokes the original
                    // tool verbatim. A NEW chip is rendered for the replay — the
                    // historical chip stays as a record of the previous attempt.
                    WSClient.getInstance().send({
                        event_type: 'client_retry_tool',
                        data: {
                            session_id: session.id,
                            tool_call_id: data.tool_call_id as string,
                        },
                    });
                    break;
                case 'INVOKE_TRACKED_BASH':
                    // Phase 7.11.6 — dev smoke command driven by the palette
                    // (`/dev/run-bash <cmd>`). Routes through the tracked tool
                    // path so the chip pipeline is provably alive end-to-end.
                    WSClient.getInstance().send({
                        event_type: 'client_invoke_tracked_bash',
                        data: {
                            session_id: session.id,
                            command: data.command as string,
                            timeout_sec: 30.0,
                            working_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
                        },
                    });
                    break;
                case 'PROMPT_FOR_BASH': {
                    // Phase 7.11.6 — palette companion: open a native VS Code
                    // input box for the command, then dispatch the smoke event.
                    const cmd = await vscode.window.showInputBox({
                        title: 'AILIENANT: run tracked bash',
                        prompt: 'Command to execute through the sandbox adapter',
                        placeHolder: 'e.g. ls -la',
                        ignoreFocusOut: false,
                    });
                    if (cmd && cmd.trim().length > 0) {
                        WSClient.getInstance().send({
                            event_type: 'client_invoke_tracked_bash',
                            data: {
                                session_id: session.id,
                                command: cmd,
                                timeout_sec: 30.0,
                                working_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
                            },
                        });
                    }
                    break;
                }
                case 'BRANCH_FROM_CHECKPOINT':
                    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: relay the
                    // user's branch request to the backend. The backend mints
                    // the new session_id and broadcasts `server_session_branched`,
                    // which the WS handler below picks up to mint the host-side
                    // Session and open it.
                    WSClient.getInstance().send({
                        event_type: 'client_branch_from_checkpoint',
                        data: {
                            parent_session_id: data.session_id as string,
                            from_checkpoint_id: data.checkpoint_id as string,
                        },
                    });
                    break;
                case 'LIST_CHECKPOINTS': {
                    // Phase 7.11.8 — fetch the checkpoint chain via REST. The
                    // backend's `/api/v1/sessions/{id}/checkpoints` endpoint
                    // returns chronological [{checkpoint_id, parent_id,
                    // promoted_at, termination_reason, turn_index}].
                    const targetSid = (data.session_id as string) || session.id;
                    try {
                        const base = WSClient.getInstance().getHttpBaseUrl();
                        const url = `${base}/api/v1/sessions/${encodeURIComponent(targetSid)}/checkpoints`;
                        const resp = await fetch(url);
                        const entries = resp.ok ? await resp.json() : [];
                        panel.webview.postMessage({ type: 'CHECKPOINTS_LIST', payload: entries });
                    } catch {
                        // Network error → empty list; the picker shows an
                        // "no checkpoints yet" empty state with no false alarm.
                        panel.webview.postMessage({ type: 'CHECKPOINTS_LIST', payload: [] });
                    }
                    break;
                }
                case 'HITL_RESPONSE':
                    WSClient.getInstance().send({
                        event_type: 'client_hitl_response',
                        data: {
                            approval_id:      data.approval_id,
                            approved:         data.approved,
                            comment:          data.comment,
                            modified_content: data.modified_content,
                        },
                    });
                    // Phase 7.11.7 — in-chat resolution wins the race: a later
                    // click on the still-visible native toast for this same
                    // approval_id becomes a no-op (idempotent guard).
                    if (typeof data.approval_id === 'string') {
                        hitlNotifier.markResolved(data.approval_id);
                    }
                    break;
                case 'NATT_MESSAGE':
                    WSClient.getInstance().send({
                        event_type: 'client_analyst_query',
                        data: {
                            text: data.text,
                            session_id: data.session_id,
                            ...(typeof data.model_tier === 'string' && { model_tier: data.model_tier }),
                            ...(Array.isArray(data.context_paths) && data.context_paths.length > 0
                                && { context_paths: data.context_paths }),
                        },
                    });
                    break;
                case 'NATT_VISIBILITY':
                    if (Boolean(data.open)) {
                        this._nattOpen.add(session.id);
                    } else {
                        this._nattOpen.delete(session.id);
                    }
                    break;
                case 'dreaming_toggle': {
                    // Dreaming enable/disable is a persisted client preference. The
                    // backend has no dreaming-enabled signal to receive — manual
                    // consolidation runs go through `client_dreaming_run`
                    // (TRIGGER_DREAMING_RUN). It must NOT touch the planner registry.
                    const enabled = data.value as boolean;
                    const profile = data.profile as DreamingProfile;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingEnabled, enabled);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingProfile, profile);
                    break;
                }
                case 'TRIGGER_DREAMING_RUN': {
                    // Manual memory consolidation. focus_area null = "Auto" (whole workspace).
                    const focusArea = (data.focus_area as string | null | undefined) ?? null;
                    WSClient.getInstance().send({
                        event_type: 'client_dreaming_run',
                        data: { focus_area: focusArea },
                    });
                    break;
                }
                case 'ATTACH_CONTEXT':
                    WSClient.getInstance().send({
                        event_type: 'client_attach_context',
                        data: { kind: data.kind, payload: data.payload },
                    });
                    break;
                case 'SET_BUDGET_LIMIT': {
                    const mode       = data.mode       as BudgetLimitMode;
                    const weeklyUsd  = data.weeklyUsd  as number;
                    const monthlyUsd = data.monthlyUsd as number;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.budgetLimitMode, mode);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.budgetWeeklyUsd, weeklyUsd);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.budgetMonthlyUsd, monthlyUsd);
                    for (const p of this._panels.values()) {
                        p.webview.postMessage({ type: 'BUDGET_UPDATED', mode, weeklyUsd, monthlyUsd });
                    }
                    break;
                }
                case 'RESTART_BACKEND': {
                    if (this._coreManager) {
                        void this._coreManager.restart();
                    }
                    break;
                }
                case 'OPEN_DASHBOARD': {
                    const cfg = vscode.workspace.getConfiguration('ailienant');
                    const base = this._coreManager?.port
                        ? `http://127.0.0.1:${this._coreManager.port}`
                        : cfg.get<string>('backendUrl', 'http://localhost:8000').replace(/\/$/, '');
                    const tab = typeof data.tab === 'string' ? data.tab : '';
                    const url = tab
                        ? `${base}/dashboard/?tab=${encodeURIComponent(tab)}`
                        : `${base}/dashboard/`;
                    void vscode.env.openExternal(vscode.Uri.parse(url));
                    break;
                }
                case 'OPEN_SETTINGS': {
                    void vscode.commands.executeCommand('workbench.action.openSettings', 'ailienant');
                    break;
                }
                case 'OPEN_DOCS': {
                    const cfg = vscode.workspace.getConfiguration('ailienant');
                    const docsUrl = cfg.get<string>('docsUrl', '').trim();
                    if (docsUrl) {
                        void vscode.env.openExternal(vscode.Uri.parse(docsUrl));
                    } else {
                        void vscode.window.showInformationMessage(
                            'AILIENANT documentation link is not configured. Set "ailienant.docsUrl" in VS Code Settings.',
                            'Open Settings',
                        ).then((choice) => {
                            if (choice === 'Open Settings') {
                                void vscode.commands.executeCommand('workbench.action.openSettings', 'ailienant.docsUrl');
                            }
                        });
                    }
                    break;
                }
                case 'MENTION_FILE': {
                    const found = await vscode.workspace.findFiles('**/*', '**/{node_modules,.git,dist,.venv}/**', 500);
                    if (found.length === 0) {
                        void vscode.window.showInformationMessage('AILIENANT: no workspace files found to mention.');
                        break;
                    }
                    const items = found.map(u => vscode.workspace.asRelativePath(u));
                    const picked = await vscode.window.showQuickPick(items.sort(), {
                        title: 'Mention a project file',
                        placeHolder: 'Type to filter workspace files',
                        matchOnDetail: true,
                    });
                    if (picked) {
                        panel.webview.postMessage({ type: 'INSERT_MENTION', path: picked });
                    }
                    break;
                }
                case 'OPEN_FILE': {
                    // Rich Plan panel file-link → open the file in the editor. The
                    // path is LLM-authored, so resolve it strictly under the
                    // workspace root and reject escapes. showTextDocument rejects
                    // for a file the plan names but hasn't created yet; an
                    // unhandled rejection would crash the host, so catch and warn.
                    const rel = typeof data.path === 'string' ? data.path : '';
                    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                    if (!rel || !root) { break; }
                    const target = vscode.Uri.joinPath(root, rel);
                    const rootPrefix = root.fsPath.endsWith('\\') || root.fsPath.endsWith('/')
                        ? root.fsPath
                        : root.fsPath + (process.platform === 'win32' ? '\\' : '/');
                    if (!target.fsPath.startsWith(rootPrefix)) { break; }  // path escapes the workspace
                    try {
                        await vscode.window.showTextDocument(target, { preview: true });
                    } catch {
                        void vscode.window.showWarningMessage(
                            `AILIENANT: can't open "${rel}" — it may not exist yet.`,
                        );
                    }
                    break;
                }
                case 'TOKENIZE_CODE': {
                    // Syntax-highlight chat code blocks host-side: the dumb webview
                    // has no grammar engine, so it ships each fenced block here for
                    // tokenization. Each block is fault-isolated (an invalid-syntax
                    // block yields a null result, not a failed batch), and the whole
                    // handler is guarded so a lexer fault never crashes the host. The
                    // reply echoes turn_id/request_id so the webview can drop a stale
                    // reply (cleared/replaced turn).
                    const turn_id = typeof data.turn_id === 'string' ? data.turn_id : '';
                    const request_id = typeof data.request_id === 'string' ? data.request_id : '';
                    const blocks = Array.isArray(data.blocks)
                        ? (data.blocks as { hash: string; lang: string; code: string }[])
                        : [];
                    try {
                        const results = await Promise.all(blocks.map(async (b) => {
                            try {
                                const ast_lines = await GrammarLexer.tokenizeByLang(b.code, b.lang);
                                return { hash: b.hash, ast_lines: ast_lines ?? null };
                            } catch {
                                return { hash: b.hash, ast_lines: null };
                            }
                        }));
                        panel.webview.postMessage({
                            type: 'CODE_TOKENS',
                            payload: { turn_id, request_id, results },
                        });
                    } catch {
                        // Whole-batch fault: keep the plain-text fallback, never crash.
                    }
                    break;
                }
                case 'CLEAR_CONVERSATION': {
                    // Phase 7.9.B.15 — also drop the backend's short-term memory.
                    WSClient.getInstance().send({ event_type: 'client_clear_conversation', data: {} });
                    panel.webview.postMessage({ type: 'CONVERSATION_CLEARED' });
                    // Phase 7.9.B.20 — and the persisted transcript for this session.
                    this.clearTranscript(session.id);
                    this._latestPlan.delete(session.id);
                    break;
                }
                case 'PERSIST_TRANSCRIPT': {
                    // Phase 7.9.B.20 — save the per-session transcript so closing VS Code
                    // no longer empties the session.
                    const msgs = Array.isArray(data.messages) ? data.messages as StoredMessage[] : [];
                    const natt = Array.isArray(data.nattMessages) ? data.nattMessages as StoredNattMessage[] : [];
                    this._saveTranscript(session.id, { messages: msgs, nattMessages: natt });
                    break;
                }
                case 'GET_MODELS': {
                    const models = await APIClient.getInstance().fetchAvailableModels();
                    panel.webview.postMessage({ type: 'MODELS_LIST', models });
                    break;
                }
                case 'GET_USAGE': {
                    const usage = await APIClient.getInstance().fetchTokenUsage();
                    panel.webview.postMessage({ type: 'USAGE_SNAPSHOT', usage });
                    break;
                }
                case 'GET_BYOM_CONFIG': {
                    const byomData = await APIClient.getInstance().fetchBYOMConfig();
                    panel.webview.postMessage({ type: 'BYOM_CONFIG', data: byomData });
                    break;
                }
                case 'ACTIVATE_PRESET': {
                    const presetId = (data.presetId as string | undefined) ?? '';
                    const result = await APIClient.getInstance().saveBYOMConfig({ active_preset_id: presetId });
                    panel.webview.postMessage({ type: 'BYOM_CONFIG', data: result });
                    break;
                }
                case 'SET_MODEL_PREFERENCE': {
                    const activeModelId     = (data.activeModelId as string | undefined) ?? '';
                    const orchestrationMode = (data.orchestrationMode as OrchestrationMode | undefined) ?? 'auto';
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.activeModelId, activeModelId);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.orchestrationMode, orchestrationMode);
                    break;
                }
                case 'OPEN_WORKSPACE': {
                    void vscode.commands.executeCommand('vscode.openFolder');
                    break;
                }
                case 'PICK_FILES': {
                    const uris = await vscode.window.showOpenDialog({
                        canSelectFiles: true,
                        canSelectFolders: false,
                        canSelectMany: true,
                        title: 'Add files to context',
                    });
                    if (uris && uris.length > 0) {
                        const items = uris.map(u => ({ path: u.fsPath, kind: 'file' as const }));
                        panel.webview.postMessage({ type: 'PICKED_PATHS', items });
                    }
                    break;
                }
                case 'PICK_FOLDER': {
                    const uris = await vscode.window.showOpenDialog({
                        canSelectFiles: false,
                        canSelectFolders: true,
                        canSelectMany: false,
                        title: 'Add folder to context',
                    });
                    if (uris && uris.length > 0) {
                        const items = uris.map(u => ({ path: u.fsPath, kind: 'directory' as const }));
                        panel.webview.postMessage({ type: 'PICKED_PATHS', items });
                    }
                    break;
                }
                case 'PICK_NATT_FILES': {
                    const uris = await vscode.window.showOpenDialog({
                        canSelectFiles: true,
                        canSelectFolders: false,
                        canSelectMany: true,
                        title: 'Attach files to Natt context',
                    });
                    if (uris && uris.length > 0) {
                        const items = uris.map(u => ({ path: u.fsPath, kind: 'file' as const }));
                        panel.webview.postMessage({ type: 'PICKED_NATT_PATHS', items });
                    }
                    break;
                }
                case 'PICK_NATT_FOLDER': {
                    const uris = await vscode.window.showOpenDialog({
                        canSelectFiles: false,
                        canSelectFolders: true,
                        canSelectMany: false,
                        title: 'Attach folder to Natt context',
                    });
                    if (uris && uris.length > 0) {
                        const items = uris.map(u => ({ path: u.fsPath, kind: 'directory' as const }));
                        panel.webview.postMessage({ type: 'PICKED_NATT_PATHS', items });
                    }
                    break;
                }
                // ── Phase 7.9.A.7 — Command-menu config (Permissions / Output styles /
                //    Agents / Hooks / MCP / Skills). All go host → APIClient → backend. ──
                case 'GET_SYSTEM_SETTINGS': {
                    const settings = await APIClient.getInstance().getSystemSettings();
                    panel.webview.postMessage({ type: 'SYSTEM_SETTINGS', data: settings });
                    break;
                }
                case 'SET_OUTPUT_STYLE': {
                    const settings = await APIClient.getInstance().saveSystemSettings({ output_style: String(data.style ?? 'default') });
                    panel.webview.postMessage({ type: 'SYSTEM_SETTINGS', data: settings });
                    break;
                }
                case 'SET_PERMISSION_MODE': {
                    const settings = await APIClient.getInstance().saveSystemSettings({ permission_mode: String(data.mode ?? 'default') });
                    panel.webview.postMessage({ type: 'SYSTEM_SETTINGS', data: settings });
                    break;
                }
                case 'GET_HOOKS': {
                    const res = await APIClient.getInstance().getHooks();
                    panel.webview.postMessage({ type: 'HOOKS_DATA', hooks: res?.hooks ?? [] });
                    break;
                }
                case 'SAVE_HOOK': {
                    const res = await APIClient.getInstance().saveHook((data.hook as Record<string, unknown>) ?? {});
                    panel.webview.postMessage({ type: 'HOOKS_DATA', hooks: res?.hooks ?? [] });
                    break;
                }
                case 'DELETE_HOOK': {
                    const res = await APIClient.getInstance().deleteHook(String(data.id ?? ''));
                    panel.webview.postMessage({ type: 'HOOKS_DATA', hooks: res?.hooks ?? [] });
                    break;
                }
                case 'GET_AGENT_ROLES': {
                    const roles = await APIClient.getInstance().getAgentRoles();
                    panel.webview.postMessage({ type: 'AGENT_ROLES', data: roles });
                    break;
                }
                case 'SAVE_AGENT_ROLE': {
                    await APIClient.getInstance().saveAgentRole(String(data.role ?? ''), String(data.system_prompt ?? ''));
                    const roles = await APIClient.getInstance().getAgentRoles();
                    panel.webview.postMessage({ type: 'AGENT_ROLES', data: roles });
                    break;
                }
                case 'GET_MCP_SERVERS': {
                    const res = await APIClient.getInstance().getMcpServers();
                    panel.webview.postMessage({ type: 'MCP_SERVERS', servers: res?.servers ?? [] });
                    break;
                }
                case 'SAVE_MCP_SERVER': {
                    const res = await APIClient.getInstance().saveMcpServer((data.server as Record<string, unknown>) ?? {});
                    panel.webview.postMessage({ type: 'MCP_SERVERS', servers: res?.servers ?? [] });
                    break;
                }
                case 'DELETE_MCP_SERVER': {
                    const res = await APIClient.getInstance().deleteMcpServer(String(data.id ?? ''));
                    panel.webview.postMessage({ type: 'MCP_SERVERS', servers: res?.servers ?? [] });
                    break;
                }
                case 'TEST_MCP_SERVER': {
                    const result = await APIClient.getInstance().testMcpServer(String(data.uri ?? ''));
                    panel.webview.postMessage({ type: 'MCP_TEST_RESULT', id: data.id, result });
                    break;
                }
                case 'GET_SKILLS': {
                    const res = await APIClient.getInstance().getSkills();
                    panel.webview.postMessage({ type: 'SKILLS_DATA', skills: res?.skills ?? [] });
                    break;
                }
                case 'SAVE_SKILL': {
                    const res = await APIClient.getInstance().saveSkill((data.skill as Record<string, unknown>) ?? {});
                    panel.webview.postMessage({ type: 'SKILLS_DATA', skills: res?.skills ?? [] });
                    break;
                }
                case 'DELETE_SKILL': {
                    const res = await APIClient.getInstance().deleteSkill(String(data.id ?? ''));
                    panel.webview.postMessage({ type: 'SKILLS_DATA', skills: res?.skills ?? [] });
                    break;
                }
            }
        });

        // ── Cleanup on tab close ────────────────────────────────────────────
        panel.onDidDispose(() => {
            WSClient.getInstance().removeMessageHandler(session.id, wsMsgHandler);
            WSClient.getInstance().removeStatusHandler(wsStatusHandler);
            // Stop announcing this session so a later reconnect doesn't re-alias a
            // closed panel onto the socket.
            WSClient.getInstance().unregisterSession(session.id);
            this._panels.delete(session.id);
            this._sessions.delete(session.id);
            this._runningTasks.delete(session.id);
            this._nattOpen.delete(session.id);
        });

        this._panels.set(session.id, panel);
        this._sessions.set(session.id, session);
    }

    /**
     * On the first SUBMIT_TASK for a session with empty title, fire
     * /api/v1/title/generate against the small-tier LLM. Fire-and-forget.
     */
    private _maybeAutoTitle(session: Session, prompt: string, panel: vscode.WebviewPanel): void {
        if (session.title.trim().length > 0) { return; }
        const updater = this._onTitleUpdate;
        if (!updater) { return; }

        const cfg = vscode.workspace.getConfiguration('ailienant');
        const base = this._coreManager?.port
            ? `http://127.0.0.1:${this._coreManager.port}`
            : cfg.get<string>('backendUrl', 'http://localhost:8000').replace(/\/$/, '');
        const token = this._coreManager?.token ?? '';
        void this._fetchTitle(base, prompt, token).then((title) => {
            if (title && this._sessions.has(session.id)) {
                updater(session.id, title);
                session.title = title;
                panel.title = title;
            }
        });
    }

    private async _fetchTitle(base: string, prompt: string, token: string = ''): Promise<string | undefined> {
        try {
            const url = `${base.replace(/\/$/, '')}/api/v1/title/generate`;
            const body = JSON.stringify({ prompt, max_words: 5 });
            const headers: Record<string, string> = { 'Content-Type': 'application/json' };
            if (token) { headers['X-AILIENANT-TOKEN'] = token; }
            const res = await fetch(url, { method: 'POST', headers, body });
            if (!res.ok) { return undefined; }
            const json = await res.json() as { title?: string };
            return json.title?.trim() || undefined;
        } catch {
            return undefined;
        }
    }

    /**
     * Fire a native VS Code notification when:
     *  - server_hitl_approval_request arrives, OR
     *  - server_natt_message with is_alert=true arrives,
     * AND the Natt pane is currently closed for this session.
     */
    private _maybeFireCriticalNotif(
        msg: { event_type?: string; data?: unknown },
        sessionId: string,
        panel: vscode.WebviewPanel,
    ): void {
        const eventType = msg.event_type;
        const isHitl = eventType === 'server_hitl_approval_request';
        const data = (msg.data ?? {}) as Record<string, unknown>;
        const isAlertNatt = eventType === 'server_natt_message' && Boolean(data.is_alert);
        if (!isHitl && !isAlertNatt) { return; }
        if (this._nattOpen.has(sessionId)) { return; }

        const name = this._nattName();
        const preview = typeof data.preview === 'string'
            ? data.preview
            : isHitl ? 'authorization required' : 'has a critical update';
        const button = `Open ${name}`;
        void vscode.window.showInformationMessage(
            `${name}: ${preview}`,
            button,
        ).then((choice) => {
            if (choice === button) {
                panel.reveal(vscode.ViewColumn.One);
                panel.webview.postMessage({ type: 'OPEN_NATT' });
            }
        });
    }

    private _renderHtml(webview: vscode.Webview, session: Session): string {
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'workspace.js')
        );
        const styleUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'workspace.css')
        );
        const logoUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'icon-color.svg')
        );

        const transcript = this._getTranscript(session.id);
        const initial = {
            sessionId:    session.id,
            sessionTitle: session.title.trim() || 'AILIENANT',
            config:       this._configLoader.current as AilienantConfig | null,
            logoUri:      logoUri.toString(),
            budgetLimitMode:  this._workspaceState.get<BudgetLimitMode>(WORKSPACE_STATE_KEYS.budgetLimitMode, 'none'),
            budgetWeeklyUsd:  this._workspaceState.get<number>(WORKSPACE_STATE_KEYS.budgetWeeklyUsd, 20),
            budgetMonthlyUsd: this._workspaceState.get<number>(WORKSPACE_STATE_KEYS.budgetMonthlyUsd, 50),
            activeModelId:    this._workspaceState.get<string>(WORKSPACE_STATE_KEYS.activeModelId, ''),
            orchestrationMode: this._workspaceState.get<OrchestrationMode>(WORKSPACE_STATE_KEYS.orchestrationMode, 'auto'),
            workspaceFolder:  vscode.workspace.workspaceFolders?.[0]?.name ?? '',
            initialMessages:     transcript.messages,      // Phase 7.9.B.20 — restore chat
            initialNattMessages: transcript.nattMessages,  // Phase 7.9.B.20 — restore analyst
        };
        const initialAttr = JSON.stringify(initial)
            .replace(/&/g, '&amp;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;');

        return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src ${webview.cspSource}; script-src ${webview.cspSource}; style-src ${webview.cspSource} 'unsafe-inline';" />
<title>${session.title.trim() || 'AILIENANT'}</title>
<link rel="stylesheet" href="${styleUri}" />
</head>
<body>
<div id="root" data-initial='${initialAttr}' data-logo='${logoUri}'></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
