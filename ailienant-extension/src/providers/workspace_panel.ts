import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as cp from 'child_process';
import * as net from 'net';
import * as crypto from 'crypto';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { PatchActuator, type ApplyWorkspaceEditPayload } from '../core/PatchActuator';
import { WSClient, WSMessageCallback, WSStatusCallback } from '../api/ws_client';
import { BudgetLimitMode, DreamingProfile, OrchestrationMode, WORKSPACE_STATE_KEYS } from '../shared/config';
import { APIClient } from '../api/api_client';
import type { AilienantConfig, Session } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { ConfigLoader } from '../shared/config_loader';

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

interface StoredMessage { role: 'user' | 'assistant'; content: string; steps?: string[]; stepsDone?: boolean; }
interface StoredNattMessage { role: 'natt' | 'user'; content: string; }
interface StoredTranscript { messages: StoredMessage[]; nattMessages: StoredNattMessage[]; }

export class WorkspacePanelManager {
    // One panel per AILIENANT session (session.id → panel)
    private _panels: Map<string, vscode.WebviewPanel> = new Map();
    private _sessions: Map<string, Session> = new Map();
    // Track which sessions currently have a task in-flight (for conflict warnings)
    private _runningTasks: Set<string> = new Set();
    // Track which sessions have the Natt pane open (gates native notifications)
    private _nattOpen: Set<string> = new Set();
    private readonly _configLoader: ConfigLoader;
    private readonly _disposables: vscode.Disposable[] = [];
    private _onTitleUpdate: TitleUpdater | undefined;
    // Phase 7.9.A.5.1 — managed child process; set from activate() via setCoreManager().
    private _coreManager: CoreProcessManager | null = null;

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
    }

    public setTitleUpdater(updater: TitleUpdater): void {
        this._onTitleUpdate = updater;
    }

    public setCoreManager(manager: CoreProcessManager): void {
        this._coreManager = manager;
    }

    public dispose(): void {
        this._coreManager?.dispose();
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
    private async _ensureBackend(): Promise<void> {
        const api = APIClient.getInstance();
        if (await api.checkHealth()) {
            SessionManager.getInstance().ensureConnected();
            return;
        }
        if (!this._coreManager) { return; }
        for (let i = 0; i < 30; i++) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            if (await api.checkHealth()) {
                SessionManager.getInstance().ensureConnected();
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
                retainContextWhenHidden: true,
                localResourceRoots: [
                    vscode.Uri.joinPath(this._extensionUri, 'dist'),
                    vscode.Uri.joinPath(this._extensionUri, 'media'),
                ],
            },
        );
        panel.iconPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'icon-color.svg');

        panel.webview.html = this._renderHtml(panel.webview, session);

        // ── WS status → forward to this panel ──────────────────────────────
        // Phase 7.9.B.20 — on (re)connect, re-seed the backend's short-term memory
        // from this session's persisted transcript so a reopened session keeps
        // conversational continuity. Seed-if-absent on the server; sent once per
        // connection so an in-flight conversation is never clobbered.
        let _historyRestored = false;
        const wsStatusHandler: WSStatusCallback = (status) => {
            panel.webview.postMessage({ type: 'WS_STATUS', payload: status });
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

            // Phase 7.9.B.18 — Enterprise Write Pipeline: the host (not the webview)
            // actuates approved patches via vscode.workspace.applyEdit, then acks.
            if (msg.event_type === 'server_apply_workspace_edit') {
                void PatchActuator.apply(msg.data as ApplyWorkspaceEditPayload).then((result) => {
                    WSClient.getInstance().send({ event_type: 'client_patch_applied', data: result });
                });
                return;
            }

            panel.webview.postMessage({ type: msg.event_type, payload: msg.data });
            this._maybeFireCriticalNotif(msg, session.id, panel);
            // Clear running-task marker on stream/task completion
            if (
                msg.event_type === 'server_stream_end' ||
                msg.event_type === 'server_task_complete'
            ) {
                this._runningTasks.delete(session.id);
            }
        };
        WSClient.getInstance().onMessage(wsMsgHandler);

        // ── Health-aware activation: connect now or auto-start the Core ──────
        void this._ensureBackend();

        // ── Panel → extension host messages ────────────────────────────────
        panel.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    this._maybeAutoTitle(session, data.value as string, panel);
                    // Inform this session if others are running (educational, non-blocking)
                    const parallelCount = [...this._runningTasks].filter(id => id !== session.id).length;
                    if (parallelCount > 0) {
                        panel.webview.postMessage({
                            type: 'PARALLEL_SESSION_NOTIFY',
                            count: parallelCount,
                        });
                    }
                    this._runningTasks.add(session.id);
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(data.value as string, activeDoc);
                    if (!intercepted) {
                        await SessionManager.getInstance().startAITask(data.value as string);
                    }
                    break;
                }
                case 'ABORT_TASK':
                    this._runningTasks.delete(session.id);
                    SessionManager.getInstance().abortCurrentTask();
                    break;
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
                    break;
                case 'NATT_MESSAGE':
                    WSClient.getInstance().send({
                        event_type: 'client_analyst_query',
                        data: {
                            text: data.text,
                            session_id: data.session_id,
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
                    const enabled = data.value as boolean;
                    const profile = data.profile as DreamingProfile;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingEnabled, enabled);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingProfile, profile);
                    WSClient.getInstance().send({
                        event_type: 'client_planner_mode_toggle',
                        data: { active: enabled, dreaming_profile: profile },
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
                case 'CLEAR_CONVERSATION': {
                    // Phase 7.9.B.15 — also drop the backend's short-term memory.
                    WSClient.getInstance().send({ event_type: 'client_clear_conversation', data: {} });
                    panel.webview.postMessage({ type: 'CONVERSATION_CLEARED' });
                    // Phase 7.9.B.20 — and the persisted transcript for this session.
                    this.clearTranscript(session.id);
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
            WSClient.getInstance().removeMessageHandler(wsMsgHandler);
            WSClient.getInstance().removeStatusHandler(wsStatusHandler);
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
