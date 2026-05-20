import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { WSClient } from '../api/ws_client';
import { DreamingProfile, WORKSPACE_STATE_KEYS } from '../shared/config';
import type { AilienantConfig, Session } from '../shared/types';
import { ConfigLoader } from '../shared/config_loader';

export class WorkspacePanelManager {
    private _panel: vscode.WebviewPanel | undefined;
    private _activeSession: Session | undefined;
    private readonly _configLoader: ConfigLoader;
    private readonly _disposables: vscode.Disposable[] = [];

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _workspaceState: vscode.Memento,
    ) {
        this._configLoader = new ConfigLoader();
        this._disposables.push(this._configLoader);

        this._configLoader.onChange((cfg) => {
            this._panel?.webview.postMessage({ type: 'CONFIG_UPDATED', config: cfg });
        });
    }

    public dispose(): void {
        for (const d of this._disposables) { d.dispose(); }
        this._panel?.dispose();
    }

    public openSession(session: Session): void {
        this._activeSession = session;
        if (this._panel) {
            this._panel.reveal(vscode.ViewColumn.One);
            this._panel.title = `AILIENANT — ${session.title}`;
            this._panel.webview.postMessage({ type: 'LOAD_SESSION', session_id: session.id });
            return;
        }
        this._createPanel(session);
    }

    private _createPanel(session: Session): void {
        const panel = vscode.window.createWebviewPanel(
            'ailienant.workspace',
            `AILIENANT — ${session.title}`,
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

        // Forward WS status + server events to the workspace webview
        WSClient.getInstance().onStatus(status => {
            panel.webview.postMessage({ type: 'WS_STATUS', payload: status });
        });
        WSClient.getInstance().onMessage((raw) => {
            const msg = raw as { event_type?: string; data?: unknown };
            if (msg.event_type) {
                panel.webview.postMessage({ type: msg.event_type, payload: msg.data });
            }
        });

        panel.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(data.value, activeDoc);
                    if (!intercepted) {
                        await SessionManager.getInstance().startAITask(data.value as string);
                    }
                    break;
                }
                case 'ABORT_TASK':
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

        panel.onDidDispose(() => {
            this._panel = undefined;
        });

        this._panel = panel;
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
            sessionTitle: session.title,
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
<title>AILIENANT — ${session.title}</title>
<link rel="stylesheet" href="${styleUri}" />
</head>
<body>
<div id="root" data-initial='${initialAttr}' data-logo='${logoUri}'></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
