import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { WSClient } from '../api/ws_client';
import {
    DEFAULT_PROFILE,
    IntelligenceProfile,
    WORKSPACE_STATE_KEYS,
} from '../shared/config';

interface InitialState {
    masterEnabled: boolean;
    profile: IntelligenceProfile;
}

export class AilienantChatProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'ailienant.chatView';
    private _view?: vscode.WebviewView;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _workspaceState: vscode.Memento,
    ) { }

    /**
     * Se ejecuta cuando el usuario abre el panel lateral por primera vez.
     */
    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(data.value, activeDoc);
                    if (!intercepted) {
                        const session = SessionManager.getInstance();
                        await session.startAITask(data.value);
                    }
                    break;
                }
                case 'ABORT_TASK': {
                    SessionManager.getInstance().abortCurrentTask();
                    break;
                }
                case 'togglePlannerMode': {
                    WSClient.getInstance().send({
                        event_type: 'client_planner_mode_toggle',
                        data: { active: data.value as boolean },
                    });
                    break;
                }
                case 'master_toggle': {
                    const enabled = data.value as boolean;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.masterEnabled, enabled);
                    WSClient.getInstance().send({
                        event_type: 'client_master_toggle',
                        data: { enabled },
                    });
                    break;
                }
                case 'profile_change': {
                    const profile = data.value as IntelligenceProfile;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.profile, profile);
                    WSClient.getInstance().send({
                        event_type: 'client_profile_change',
                        data: { profile },
                    });
                    break;
                }
            }
        });
    }

    /**
     * Envía datos DESDE la extensión HACIA la UI (React).
     */
    public sendMessageToWebview(type: string, payload: unknown) {
        if (this._view) {
            this._view.webview.postMessage({ type, payload });
        }
    }

    private _readInitialState(): InitialState {
        return {
            masterEnabled: this._workspaceState.get<boolean>(
                WORKSPACE_STATE_KEYS.masterEnabled, false,
            ),
            profile: this._workspaceState.get<IntelligenceProfile>(
                WORKSPACE_STATE_KEYS.profile, DEFAULT_PROFILE,
            ),
        };
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview.js')
        );
        // Encode initial state on a data-* attribute (CSP-safe — no inline <script>).
        const initialAttr = JSON.stringify(this._readInitialState())
            .replace(/&/g, '&amp;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;');
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none'; script-src ${webview.cspSource};">
    <title>AILIENANT Chat</title>
</head>
<body>
    <div id="root" data-initial='${initialAttr}'></div>
    <script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
