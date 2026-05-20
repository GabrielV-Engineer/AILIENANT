import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { WSClient, WSMessageCallback, WSStatusCallback } from '../api/ws_client';
import { DreamingProfile, WORKSPACE_STATE_KEYS } from '../shared/config';
import type { AilienantConfig, Session } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { ConfigLoader } from '../shared/config_loader';

interface TitleUpdater {
    (sessionId: string, title: string): void;
}

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
    }

    public setTitleUpdater(updater: TitleUpdater): void {
        this._onTitleUpdate = updater;
    }

    public dispose(): void {
        for (const d of this._disposables) { d.dispose(); }
        for (const panel of this._panels.values()) { panel.dispose(); }
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

        panel.webview.html = this._renderHtml(panel.webview, session);

        // ── WS status → forward to this panel ──────────────────────────────
        const wsStatusHandler: WSStatusCallback = (status) => {
            panel.webview.postMessage({ type: 'WS_STATUS', payload: status });
        };
        WSClient.getInstance().onStatus(wsStatusHandler);

        // ── WS messages → route to this panel only ──────────────────────────
        // Events with a matching session_id go here; events without one broadcast.
        const wsMsgHandler: WSMessageCallback = (raw) => {
            const msg = raw as { event_type?: string; data?: unknown };
            if (!msg.event_type) { return; }
            const data = msg.data as Record<string, unknown> | undefined;
            if (data?.session_id && data.session_id !== session.id) { return; }
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
                        data: { text: data.text, session_id: data.session_id },
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
        const base = cfg.get<string>('backendUrl', 'http://localhost:8000');
        void this._fetchTitle(base, prompt).then((title) => {
            if (title && this._sessions.has(session.id)) {
                updater(session.id, title);
                session.title = title;
                panel.title = title;
            }
        });
    }

    private async _fetchTitle(base: string, prompt: string): Promise<string | undefined> {
        try {
            const url = `${base.replace(/\/$/, '')}/api/v1/title/generate`;
            const body = JSON.stringify({ prompt, max_words: 5 });
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body,
            });
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
            vscode.Uri.joinPath(this._extensionUri, 'media', 'logo.svg')
        );

        const initial = {
            sessionId:    session.id,
            sessionTitle: session.title.trim() || 'AILIENANT',
            config:       this._configLoader.current as AilienantConfig | null,
            logoUri:      logoUri.toString(),
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
